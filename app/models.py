import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance >= 0", name="balance_nonnegative"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    balance: Mapped[int] = mapped_column(BigInteger, server_default="0")


class Model(Base):
    __tablename__ = "models"
    __table_args__ = (CheckConstraint("cost > 0", name="model_positive_cost"),)
    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    cost: Mapped[int]


class Leg(Base):
    __tablename__ = "legs"
    __table_args__ = (
        UniqueConstraint("model_slug", "position", name="uq_model_position"),
        CheckConstraint("provider IN ('alpha', 'beta')", name="leg_provider"),
        CheckConstraint("position > 0 AND max_ref_images >= 0", name="leg_limits"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    model_slug: Mapped[str] = mapped_column(ForeignKey("models.slug"))
    position: Mapped[int]
    provider: Mapped[str] = mapped_column(String(16))
    max_ref_images: Mapped[int]
    skip_on_moderation: Mapped[bool] = mapped_column(server_default="false")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('active','completed','failed')", name="task_status"),
        CheckConstraint("cost > 0", name="task_cost"),
        CheckConstraint(
            "(status = 'active' AND finished_at IS NULL) OR "
            "(status <> 'active' AND finished_at IS NOT NULL)",
            name="task_terminal_time",
        ),
        Index("ix_tasks_user_status", "user_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    model_slug: Mapped[str] = mapped_column(ForeignKey("models.slug"))
    prompt: Mapped[str]
    ref_images: Mapped[list] = mapped_column(JSONB)
    cost: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="active")
    current_leg: Mapped[int]
    moderation_seen: Mapped[bool] = mapped_column(default=False)
    result_url: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "leg_position", name="uq_attempt_leg"),
        CheckConstraint(
            "status IN ('queued','submitted','completed','failed')", name="attempt_status"
        ),
        Index(
            "ix_attempts_deadline",
            "deadline",
            postgresql_where=text("status IN ('queued','submitted')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    leg_position: Mapped[int]
    provider: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    external_id: Mapped[str | None]
    error_type: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ledger(Base):
    __tablename__ = "ledger"
    __table_args__ = (
        UniqueConstraint("payment_id", name="uq_ledger_payment"),
        UniqueConstraint("task_id", "kind", name="uq_ledger_task_kind"),
        CheckConstraint(
            "(kind = 'payment' AND amount > 0 AND payment_id IS NOT NULL AND task_id IS NULL) OR "
            "(kind = 'debit' AND amount < 0 AND task_id IS NOT NULL AND payment_id IS NULL) OR "
            "(kind = 'refund' AND amount > 0 AND task_id IS NOT NULL AND payment_id IS NULL)",
            name="ledger_shape",
        ),
        Index("ix_ledger_user_id", "user_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    payment_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "done", "lease_until"),)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id"), primary_key=True)
    done: Mapped[bool] = mapped_column(server_default="false")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None]
    deliveries: Mapped[int] = mapped_column(server_default="0")


class ProviderSetting(Base):
    __tablename__ = "provider_settings"
    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(32))
    delay: Mapped[float | None]


class ProviderJob(Base):
    __tablename__ = "provider_jobs"
    __table_args__ = (
        Index("ix_provider_jobs_due", "provider", "delivered", "due_at", "lease_until"),
    )
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    scenario: Mapped[str] = mapped_column(String(32))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered: Mapped[bool] = mapped_column(server_default="false")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None]
    deliveries: Mapped[int] = mapped_column(server_default="0")
