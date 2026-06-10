from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class SettingValueType(str, Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    JSON = "json"
    SECRET = "secret"


class CommissionLocalStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DELETED = "deleted"


class InventoryMovementType(str, Enum):
    WITHDRAWAL = "withdrawal"
    ORDERED = "ordered"
    STORED = "stored"
    CORRECTION = "correction"


class TimeReviewStatus(str, Enum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


user_craft_activity = db.Table(
    "user_craft_activity",
    db.Column("user_id", db.ForeignKey("user_account.id"), primary_key=True),
    db.Column("craft_activity_id", db.ForeignKey("craft_activity.id"), primary_key=True),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppSetting(db.Model):
    __tablename__ = "app_setting"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(db.String(160), unique=True, nullable=False, index=True)
    value_text: Mapped[str | None] = mapped_column(db.Text)
    value_type: Mapped[str] = mapped_column(db.String(30), nullable=False)
    label: Mapped[str] = mapped_column(db.String(200), nullable=False)
    group: Mapped[str] = mapped_column(db.String(80), nullable=False, index=True)
    is_secret: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class CraftActivity(db.Model):
    __tablename__ = "craft_activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int | None] = mapped_column(db.Integer, unique=True, index=True)
    code: Mapped[str | None] = mapped_column(db.String(100), index=True)
    name: Mapped[str] = mapped_column(db.String(255), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    users: Mapped[list["User"]] = relationship(
        "User",
        secondary=user_craft_activity,
        back_populates="craft_activities",
    )

    service_positions: Mapped[list["TimeTaskTemplate"]] = relationship(back_populates="craft_activity")


class User(UserMixin, db.Model):
    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(db.String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(db.String(255), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(db.String(255))
    entra_oid: Mapped[str | None] = mapped_column(db.String(80), unique=True)
    craftsman_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    is_admin: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    is_foreman: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    is_active_flag: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    commissions: Mapped[list["Commission"]] = relationship(back_populates="user")
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="user")
    time_entries: Mapped[list["TimeEntry"]] = relationship(back_populates="user", foreign_keys="TimeEntry.user_id")
    craft_activities: Mapped[list["CraftActivity"]] = relationship(
        "CraftActivity",
        secondary=user_craft_activity,
        back_populates="users",
    )

    @property
    def is_active(self) -> bool:
        return self.is_active_flag

    def set_password(self, password: str | None) -> None:
        self.password_hash = generate_password_hash(password) if password else None

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)


class Commission(db.Model):
    __tablename__ = "commission"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(db.Integer, unique=True, nullable=False, index=True)
    erp_idnum: Mapped[str | None] = mapped_column(db.String(80), index=True)
    code: Mapped[str | None] = mapped_column(db.String(120), index=True)
    title: Mapped[str | None] = mapped_column(db.String(255))
    description: Mapped[str | None] = mapped_column(db.Text)
    damage_location: Mapped[str | None] = mapped_column(db.String(255))
    contact_person: Mapped[str | None] = mapped_column(db.String(255))
    status_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    status_code: Mapped[str | None] = mapped_column(db.String(120), index=True)
    craftsman_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    use_unit_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    license_agreement_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    apartment_address: Mapped[str | None] = mapped_column(db.String(500))
    apartment_location: Mapped[str | None] = mapped_column(db.String(255))
    tenant_name: Mapped[str | None] = mapped_column(db.String(255))
    user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    local_status: Mapped[str] = mapped_column(db.String(40), default=CommissionLocalStatus.OPEN.value, nullable=False, index=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(db.JSON)
    recording_date: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    release_date: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    execution_from: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    execution_to: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped[User | None] = relationship(back_populates="commissions")
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="commission")
    time_entries: Mapped[list["TimeEntry"]] = relationship(back_populates="commission")


class InventoryItem(db.Model):
    __tablename__ = "inventory_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_number: Mapped[str] = mapped_column(db.String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(db.String(255), nullable=False)
    unit: Mapped[str] = mapped_column(db.String(40), default="Stk", nullable=False)
    ean: Mapped[str | None] = mapped_column(db.String(80), index=True)
    qr_code_payload: Mapped[str] = mapped_column(db.String(160), unique=True, nullable=False, default=lambda: f"datlaeuft:item:{uuid4()}")
    stock_quantity: Mapped[float] = mapped_column(db.Float, default=0, nullable=False)
    reorder_level: Mapped[float] = mapped_column(db.Float, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)

    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="item")


class InventoryMovement(db.Model):
    __tablename__ = "inventory_movement"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(db.ForeignKey("inventory_item.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    commission_id: Mapped[int | None] = mapped_column(db.ForeignKey("commission.id"), index=True)
    movement_type: Mapped[str] = mapped_column(db.String(40), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(db.Float, nullable=False)
    note: Mapped[str | None] = mapped_column(db.Text)
    occurred_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False, index=True)

    item: Mapped[InventoryItem] = relationship(back_populates="movements")
    user: Mapped[User | None] = relationship(back_populates="inventory_movements")
    commission: Mapped[Commission | None] = relationship(back_populates="inventory_movements")


class TimeTaskTemplate(db.Model):
    __tablename__ = "time_task_template"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(db.String(120), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(db.String(255), nullable=False, index=True)
    suggested_minutes: Mapped[int] = mapped_column(db.Integer, default=0, nullable=False)
    craft_activity_id: Mapped[int | None] = mapped_column(db.ForeignKey("craft_activity.id"), index=True)
    erp_service_catalogue_id: Mapped[int | None] = mapped_column(db.Integer, unique=True, index=True)
    erp_idnum: Mapped[str | None] = mapped_column(db.String(120), index=True)
    source: Mapped[str] = mapped_column(db.String(40), default="manual", server_default="manual", nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    craft_activity: Mapped[CraftActivity | None] = relationship(back_populates="service_positions")


class TimeEntry(db.Model):
    __tablename__ = "time_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(db.ForeignKey("commission.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(db.ForeignKey("user_account.id"), nullable=False, index=True)
    task_template_id: Mapped[int | None] = mapped_column(db.ForeignKey("time_task_template.id"), index=True)
    started_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    minutes: Mapped[int] = mapped_column(db.Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(db.Text)
    deviation_minutes: Mapped[int] = mapped_column(db.Integer, default=0, nullable=False)
    deviation_percent: Mapped[float] = mapped_column(db.Float, default=0, nullable=False)
    review_status: Mapped[str] = mapped_column(db.String(40), default=TimeReviewStatus.OK.value, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    commission: Mapped[Commission] = relationship(back_populates="time_entries")
    user: Mapped[User] = relationship(foreign_keys=[user_id], back_populates="time_entries")
    task_template: Mapped[TimeTaskTemplate | None] = relationship()
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_user_id])


class WebhookEvent(db.Model):
    __tablename__ = "webhook_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str | None] = mapped_column(db.String(80), index=True)
    entity_name: Mapped[str | None] = mapped_column(db.String(120), index=True)
    entity_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    action: Mapped[str | None] = mapped_column(db.String(40), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(db.JSON, nullable=False)
    processed: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    message: Mapped[str | None] = mapped_column(db.Text)
    received_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    action: Mapped[str] = mapped_column(db.String(120), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(db.String(120), index=True)
    entity_id: Mapped[str | None] = mapped_column(db.String(120), index=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(db.JSON)
    ip_address: Mapped[str | None] = mapped_column(db.String(80))
    user_agent: Mapped[str | None] = mapped_column(db.String(255))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False, index=True)

    actor: Mapped[User | None] = relationship()


class ApiTokenBlocklist(db.Model):
    __tablename__ = "api_token_blocklist"
    __table_args__ = (UniqueConstraint("jti", name="uq_api_token_blocklist_jti"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    jti: Mapped[str] = mapped_column(db.String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)