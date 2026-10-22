"""Start isolated local processes and always tear them down; PostgreSQL must exist.

DATABASE_URL must target a dedicated generation_test* database. The same smoke
can be run separately against Compose with `python -m scripts.smoke`.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text

from app.config import Settings
from app.db import connect

ROOT = Path(__file__).resolve().parents[1]


async def reset_test_database(url: str) -> None:
    engine, sessions = connect(Settings(database_url=url))
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "TRUNCATE provider_jobs, provider_settings, outbox, ledger, attempts, tasks, "
                    "users RESTART IDENTITY CASCADE"
                )
            )
            await session.execute(text("INSERT INTO users(id) VALUES (1)"))
    finally:
        await engine.dispose()


def main():
    url = os.environ.get("DATABASE_URL", "")
    if not url.rsplit("/", 1)[-1].startswith("generation_test"):
        raise SystemExit("Set DATABASE_URL to an isolated generation_test* PostgreSQL database")
    env = {
        **os.environ,
        "ALPHA_URL": "http://127.0.0.1:18001",
        "BETA_URL": "http://127.0.0.1:18002",
        "CALLBACK_BASE_URL": "http://127.0.0.1:18000",
        "PROVIDER_TIMEOUT": "8",
        "HTTP_TIMEOUT": "2",
        "DISPATCH_LEASE": "4",
        "WORKER_POLL": "0.05",
        "SMOKE_API_URL": "http://127.0.0.1:18000",
        "SMOKE_ALPHA_URL": "http://127.0.0.1:18001",
        "SMOKE_BETA_URL": "http://127.0.0.1:18002",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
    )
    asyncio.run(reset_test_database(url))
    processes, logs = [], []
    log_dir = ROOT / ".tools" / "smoke"
    log_dir.mkdir(parents=True, exist_ok=True)

    def start(name, arguments, extra=None):
        log = (log_dir / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=ROOT,
            env={**env, **(extra or {})},
            stdout=log,
            stderr=subprocess.STDOUT,
            **options,
        )
        processes.append(process)

    try:
        start(
            "api",
            [
                "-m",
                "uvicorn",
                "app.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18000",
                "--workers",
                "4",
            ],
        )
        for name, port in [("alpha", "18001"), ("beta", "18002")]:
            start(
                name,
                ["-m", "uvicorn", "app.provider:app", "--host", "127.0.0.1", "--port", port],
                {"PROVIDER_NAME": name},
            )
        deadline = time.monotonic() + 40
        pending = {18000, 18001, 18002}
        while pending and time.monotonic() < deadline:
            for port in list(pending):
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                    if response.status_code == 200:
                        pending.remove(port)
                except httpx.HTTPError:
                    pass
            if any(p.poll() is not None for p in processes):
                raise RuntimeError(f"A service exited; inspect {log_dir}")
            time.sleep(0.1)
        assert not pending, f"Health checks timed out: {pending}; inspect {log_dir}"
        # Two independent workers prove that both dispatch and watchdog can compete.
        start("worker1", ["-m", "app.worker"])
        start("worker2", ["-m", "app.worker"])
        subprocess.run(
            [sys.executable, "-m", "scripts.smoke"], cwd=ROOT, env=env, check=True, timeout=90
        )
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
