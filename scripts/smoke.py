"""Real HTTP smoke against a running Compose or local stack; no database truncation."""

import asyncio
import json
import os
import time
import uuid

import httpx

from app.config import get_settings
from app.security import sign


async def smoke() -> dict:
    settings = get_settings()
    api_url = os.environ.get("SMOKE_API_URL", "http://127.0.0.1:8000")
    alpha_url = os.environ.get("SMOKE_ALPHA_URL", "http://127.0.0.1:8001")
    beta_url = os.environ.get("SMOKE_BETA_URL", "http://127.0.0.1:8002")
    async with httpx.AsyncClient(
        timeout=30, limits=httpx.Limits(max_keepalive_connections=0)
    ) as client:
        pids = set()
        for _ in range(30):
            responses = await asyncio.gather(*(client.get(api_url + "/health") for _ in range(8)))
            pids.update(r.json()["pid"] for r in responses)
            if len(pids) == 4:
                break
        assert len(pids) == 4, f"Expected exactly 4 API workers, observed {pids}"
        initial = (await client.get(api_url + "/users/1/balance")).json()["balance"]
        payload = {
            "payment_id": f"smoke-{uuid.uuid4()}",
            "user_id": 1,
            "tokens": 100,
            "status": "completed",
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        responses = await asyncio.gather(
            *(
                client.post(
                    api_url + "/webhooks/payments",
                    content=raw,
                    headers={"X-Signature": sign(raw, settings.payment_secret)},
                )
                for _ in range(20)
            )
        )
        assert all(r.status_code == 200 for r in responses)
        assert sum(r.json()["applied"] for r in responses) == 1

        async def scenario(url, name, delay=0.1):
            response = await client.post(
                url + "/admin/scenario",
                json={"scenario": name, "delay": delay},
                headers={"X-API-Key": settings.provider_api_key},
            )
            response.raise_for_status()

        async def generate():
            response = await client.post(
                api_url + "/generate",
                json={
                    "user_id": 1,
                    "model_slug": "image-fast",
                    "prompt": "smoke landscape",
                    "ref_images": [],
                },
            )
            assert response.status_code == 202, response.text
            return response.json()["task_id"]

        async def wait(task_id):
            deadline = time.monotonic() + settings.provider_timeout * 3 + 10
            while time.monotonic() < deadline:
                task = (await client.get(api_url + "/tasks/" + task_id)).json()
                if task["status"] != "active":
                    return task
                await asyncio.sleep(0.1)
            raise AssertionError(f"Task {task_id} did not terminate")

        try:
            await scenario(alpha_url, "technical")
            await scenario(beta_url, "parallel")
            fallback = await wait(await generate())
            assert fallback["status"] == "completed"
            assert [a["provider"] for a in fallback["attempts"]] == ["alpha", "beta"]

            await scenario(alpha_url, "payload")
            failed = await wait(await generate())
            assert failed["status"] == "failed" and len(failed["attempts"]) == 1

            await scenario(alpha_url, "http503")
            unavailable = await wait(await generate())
            assert unavailable["status"] == "completed"
            assert unavailable["attempts"][0]["error_type"] == "http_5xx"

            # Delay callback until POST times out, reproducing accepted-but-response-lost.
            await scenario(alpha_url, "timeout", settings.http_timeout + 1)
            timed_out = await wait(await generate())
            assert timed_out["status"] == "completed"
            assert timed_out["attempts"][0]["error_type"] == "send_timeout"

            await scenario(alpha_url, "no_webhook")
            missing = await wait(await generate())
            assert missing["status"] == "completed"
            assert missing["attempts"][0]["error_type"] == "timeout"
            expected = initial + 100 - 8
            balance = (await client.get(api_url + "/users/1/balance")).json()["balance"]
            assert balance == expected, (balance, expected)
            result = {
                "api_worker_pids": sorted(pids),
                "payment_deliveries": 20,
                "scenarios": [
                    "fallback+parallel",
                    "payload+refund",
                    "http503",
                    "accepted+send_timeout",
                    "no_webhook+watchdog",
                ],
                "balance": balance,
            }
            print(json.dumps(result, indent=2), flush=True)
            return result
        finally:
            await scenario(alpha_url, "random", None)
            await scenario(beta_url, "random", None)


if __name__ == "__main__":
    asyncio.run(smoke())
