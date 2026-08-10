from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from flask_login import UserMixin
from sqlalchemy import UniqueConstraint, false, true
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
    RECORDED = "recorded"
    WAITING_FOR_ACCEPTANCE = "waiting_for_acceptance"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETION_REVIEW = "completion_review"
    TENANT_CHARGE_REVIEW = "tenant_charge_review"
    DONE = "done"
    DELETED = "deleted"


class CommissionSource(str, Enum):
    WOWIPORT = "wowiport"
    WEB = "web"
    IOS = "ios"


class CommissionSyncState(str, Enum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"


class ErpOperationType(str, Enum):
    CREATE_COMMISSION = "create_commission"
    ACCEPT_COMMISSION = "accept_commission"
    REFUSE_COMMISSION = "refuse_commission"
    CANCEL_COMMISSION = "cancel_commission"
    COMPLETE_COMMISSION = "complete_commission"
    AWAIT_INVOICE = "await_invoice"
    UPLOAD_COMMISSION_PHOTO = "upload_commission_photo"


class ErpOutboxState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class PushEventType(str, Enum):
    NEW_COMMISSION = "new_commission"
    CHANGED_COMMISSION = "changed_commission"
    PING = "ping"


class PushDeliveryState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    DISCARDED = "discarded"


class InventoryMovementType(str, Enum):
    WITHDRAWAL = "withdrawal"
    ORDERED = "ordered"
    STORED = "stored"
    CORRECTION = "correction"
    CANCELLATION = "cancellation"
    TRANSFER = "transfer"


class AppointmentRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class InventoryLocationType(str, Enum):
    WAREHOUSE = "warehouse"
    VEHICLE = "vehicle"
    OTHER = "other"

    @property
    def label(self) -> str:
        if self == InventoryLocationType.WAREHOUSE:
            return "Lager"

        if self == InventoryLocationType.VEHICLE:
            return "Fahrzeug"

        return "Sonstiger Lagerort"


class TimeReviewStatus(str, Enum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewLevel(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class MaterialUsageStatus(str, Enum):
    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"
    RETURNED = "returned"
    DEFECTIVE = "defective"
    IN_VEHICLE = "in_vehicle"


class MaterialSource(str, Enum):
    WAREHOUSE = "warehouse"
    VEHICLE = "vehicle"
    DIRECT_PURCHASE = "direct_purchase"
    ON_SITE = "on_site"


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
    person_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    creditor_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    creditor_number: Mapped[str | None] = mapped_column(db.String(120))
    craftsman_portal_id: Mapped[str | None] = mapped_column(db.String(120))
    wowiport_managed: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    exclude_from_wowiport_sync: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    is_admin: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    is_foreman: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    is_active_flag: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    push_new_commission_enabled: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    push_changed_commission_enabled: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    commissions: Mapped[list["Commission"]] = relationship(
        back_populates="user",
        foreign_keys="Commission.user_id",
    )
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="user", foreign_keys="InventoryMovement.user_id")
    time_entries: Mapped[list["TimeEntry"]] = relationship(back_populates="user", foreign_keys="TimeEntry.user_id")
    craft_activities: Mapped[list["CraftActivity"]] = relationship(
        "CraftActivity",
        secondary=user_craft_activity,
        back_populates="users",
    )
    push_devices: Mapped[list["PushDevice"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    push_deliveries: Mapped[list["PushDelivery"]] = relationship(
        back_populates="user",
        foreign_keys="PushDelivery.user_id",
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
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: str(uuid4()),
    )
    erp_id: Mapped[int | None] = mapped_column(db.Integer, unique=True, nullable=True, index=True)
    erp_idnum: Mapped[str | None] = mapped_column(db.String(80), index=True)
    erp_external_identification_number: Mapped[str | None] = mapped_column(db.String(80))
    erp_sync_state: Mapped[str] = mapped_column(
        db.String(30),
        default=CommissionSyncState.SYNCED.value,
        server_default=CommissionSyncState.SYNCED.value,
        nullable=False,
        index=True,
    )
    erp_sync_message: Mapped[str | None] = mapped_column(db.Text)
    source: Mapped[str] = mapped_column(
        db.String(30),
        default=CommissionSource.WOWIPORT.value,
        server_default=CommissionSource.WOWIPORT.value,
        nullable=False,
        index=True,
    )
    commission_type_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    crafts_process_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    company_code_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    company_code_code: Mapped[str | None] = mapped_column(
        db.String(40),
        index=True,
    )
    crafts_process_type_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    crafts_process_type_code: Mapped[str | None] = mapped_column(
        db.String(120),
        index=True,
    )
    crafts_process_status_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    crafts_process_status_code: Mapped[str | None] = mapped_column(
        db.String(120),
        index=True,
    )
    sales_tax_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    sales_tax_code: Mapped[str | None] = mapped_column(
        db.String(40),
        index=True,
    )
    creditor_erp_id: Mapped[int | None] = mapped_column(db.Integer)
    department_erp_id: Mapped[int | None] = mapped_column(db.Integer)
    responsible_official_repair_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    code: Mapped[str | None] = mapped_column(db.String(120), index=True)
    title: Mapped[str | None] = mapped_column(db.String(255))
    description: Mapped[str | None] = mapped_column(db.Text)
    damage_location: Mapped[str | None] = mapped_column(db.String(255))
    contact_person: Mapped[str | None] = mapped_column(db.String(255))
    status_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    status_code: Mapped[str | None] = mapped_column(db.String(120), index=True)
    craftsman_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    use_unit_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    use_unit_idnum: Mapped[str | None] = mapped_column(
        db.String(100),
        index=True,
    )
    building_idnum: Mapped[str | None] = mapped_column(
        db.String(100),
        index=True,
    )
    license_agreement_idnum: Mapped[str | None] = mapped_column(
        db.String(100),
        index=True,
    )
    license_agreement_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    apartment_address: Mapped[str | None] = mapped_column(db.String(500))
    apartment_location: Mapped[str | None] = mapped_column(db.String(255))
    tenant_name: Mapped[str | None] = mapped_column(db.String(255))
    user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    local_status: Mapped[str] = mapped_column(
        db.String(40),
        default=CommissionLocalStatus.OPEN.value,
        nullable=False,
        index=True,
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(db.JSON)
    recording_date: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    release_date: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    execution_from: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    execution_to: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completion_successful: Mapped[bool | None] = mapped_column(db.Boolean)
    tenant_charge_requested: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    completion_reason: Mapped[str | None] = mapped_column(db.Text)
    completion_submitted_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    completion_submitted_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    document_synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    document_sync_error: Mapped[str | None] = mapped_column(
        db.Text
    )
    media_synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    media_sync_error: Mapped[str | None] = mapped_column(
        db.Text
    )
    building_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    latitude: Mapped[float | None] = mapped_column(db.Float)
    longitude: Mapped[float | None] = mapped_column(db.Float)

    accepted_appointment_from: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    accepted_appointment_to: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    accepted_appointment_comment: Mapped[str | None] = mapped_column(
        db.Text
    )

    user: Mapped[User | None] = relationship(
        back_populates="commissions",
        foreign_keys=[user_id],
    )
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])
    completion_submitted_by: Mapped[User | None] = relationship(
        foreign_keys=[completion_submitted_by_user_id],
    )
    positions: Mapped[list["CommissionPosition"]] = relationship(
        back_populates="commission",
        cascade="all, delete-orphan",
        order_by="CommissionPosition.position_index",
    )
    erp_operations: Mapped[list["ErpOutboxOperation"]] = relationship(
        back_populates="commission",
        cascade="all, delete-orphan",
    )
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="commission")
    time_entries: Mapped[list["TimeEntry"]] = relationship(back_populates="commission")
    photos: Mapped[list["CommissionPhoto"]] = relationship(
        back_populates="commission",
        cascade="all, delete-orphan",
    )
    attachments: Mapped[list["CommissionAttachment"]] = relationship(
        back_populates="commission",
        cascade="all, delete-orphan",
    )
    appointment_requests: Mapped[list["CommissionAppointmentRequest"]] = relationship(
        back_populates="commission",
        cascade="all, delete-orphan",
    )
    push_deliveries: Mapped[list["PushDelivery"]] = relationship(
        back_populates="commission",
    )


    @property
    def is_actionable(self) -> bool:
        return (
            self.erp_id is not None
            and bool(self.erp_idnum)
            and self.local_status not in {
                CommissionLocalStatus.RECORDED.value,
                CommissionLocalStatus.DELETED.value,
            }
        )

    @property
    def display_title(self) -> str:
        if len(self.positions) > 1:
            return "Mehrere Positionen"
        if self.title:
            return self.title
        if self.positions:
            return self.positions[0].display_text
        return "Ausstehender Auftrag"


class PushDevice(db.Model):
    __tablename__ = "push_device"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            name="uq_push_device_installation_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    installation_id: Mapped[str] = mapped_column(
        db.String(36),
        nullable=False,
        index=True,
    )
    device_token: Mapped[str] = mapped_column(
        db.String(512),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(
        db.String(30),
        default="ios",
        server_default="ios",
        nullable=False,
    )
    app_version: Mapped[str | None] = mapped_column(db.String(80))
    device_name: Mapped[str | None] = mapped_column(db.String(255))
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    last_registered_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="push_devices")
    deliveries: Mapped[list["PushDelivery"]] = relationship(
        back_populates="device",
    )


class PushDelivery(db.Model):
    __tablename__ = "push_delivery"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "device_id",
            name="uq_push_delivery_batch_device",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        db.String(36),
        nullable=False,
        index=True,
    )
    device_id: Mapped[int] = mapped_column(
        db.ForeignKey("push_device.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    commission_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("commission.id"),
        index=True,
    )
    device_token: Mapped[str] = mapped_column(
        db.String(512),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        db.String(40),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(db.String(180), nullable=False)
    body: Mapped[str] = mapped_column(db.String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(db.JSON, nullable=False)
    collapse_id: Mapped[str | None] = mapped_column(db.String(64))
    state: Mapped[str] = mapped_column(
        db.String(30),
        default=PushDeliveryState.PENDING.value,
        server_default=PushDeliveryState.PENDING.value,
        nullable=False,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    available_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(db.Text)
    provider_status: Mapped[int | None] = mapped_column(db.Integer)
    provider_reason: Mapped[str | None] = mapped_column(db.String(120))
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    device: Mapped[PushDevice] = relationship(back_populates="deliveries")
    user: Mapped[User] = relationship(
        back_populates="push_deliveries",
        foreign_keys=[user_id],
    )
    commission: Mapped[Commission | None] = relationship(
        back_populates="push_deliveries",
    )


class CommissionPosition(db.Model):
    __tablename__ = "commission_position"
    __table_args__ = (
        UniqueConstraint(
            "commission_id",
            "position_index",
            name="uq_commission_position_index",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=False,
        index=True,
    )
    erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    position_index: Mapped[int] = mapped_column(db.Integer, nullable=False)
    position_number: Mapped[float | None] = mapped_column(db.Float)
    code: Mapped[str | None] = mapped_column(db.String(120))
    commission_text: Mapped[str | None] = mapped_column(db.Text)
    internal_description: Mapped[str | None] = mapped_column(db.Text)
    description: Mapped[str | None] = mapped_column(db.Text)
    units: Mapped[float] = mapped_column(db.Float, default=1, server_default="1", nullable=False)
    unit_price: Mapped[float | None] = mapped_column(db.Float)
    net_amount: Mapped[float | None] = mapped_column(db.Float)
    material_costs: Mapped[float | None] = mapped_column(db.Float)
    labour_costs: Mapped[float | None] = mapped_column(db.Float)
    service_catalog_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    craft_activity_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)

    commission: Mapped[Commission] = relationship(back_populates="positions")

    @property
    def display_text(self) -> str:
        return (
            self.internal_description
            or self.commission_text
            or self.description
            or self.code
            or f"Position {self.position_index + 1}"
        )

    def to_erp_payload(self) -> dict[str, Any]:
        values = {
            "CraftActivityId": self.craft_activity_erp_id,
            "Code": self.code,
            "ServiceCatalogId": self.service_catalog_erp_id,
            "CommissionText": self.commission_text,
            "Units": self.units,
            "UnitPrice": self.unit_price,
            "NetAmount": self.net_amount,
            "MaterialCosts": self.material_costs,
            "LabourCosts": self.labour_costs,
            "Description": self.description,
        }
        return {key: value for key, value in values.items() if value is not None}


class CommissionPhoto(db.Model):
    __tablename__ = "commission_photo"
    __table_args__ = (
        UniqueConstraint(
            "commission_id",
            "external_id",
            name="uq_commission_photo_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=False,
        index=True,
    )
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    context: Mapped[str] = mapped_column(
        db.String(40),
        default="order",
        server_default="order",
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(
        db.String(40),
        default="app",
        server_default="app",
        nullable=False,
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(
        db.String(160),
        index=True,
    )
    external_document_id: Mapped[str] = mapped_column(
        db.String(80),
        default=lambda: str(uuid4()),
        nullable=False,
        unique=True,
        index=True,
    )
    external_entity_name: Mapped[str | None] = mapped_column(
        db.String(80),
        index=True,
    )
    external_entity_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    picture_type_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    picture_type_name: Mapped[str | None] = mapped_column(
        db.String(160)
    )
    original_filename: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
    )
    storage_name: Mapped[str] = mapped_column(
        db.String(500),
        unique=True,
        nullable=False,
    )
    thumbnail_storage_name: Mapped[str] = mapped_column(
        db.String(500),
        unique=True,
        nullable=False,
    )
    mime_type: Mapped[str] = mapped_column(
        db.String(100),
        default="image/jpeg",
        server_default="image/jpeg",
        nullable=False,
    )
    size_bytes: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(
        db.String(64),
        nullable=False,
        index=True,
    )
    caption: Mapped[str | None] = mapped_column(
        db.String(500)
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    erp_sync_state: Mapped[str] = mapped_column(
        db.String(30),
        default=CommissionSyncState.PENDING.value,
        server_default=CommissionSyncState.PENDING.value,
        nullable=False,
        index=True,
    )
    erp_sync_message: Mapped[str | None] = mapped_column(
        db.Text
    )
    erp_synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    external_updated_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    commission: Mapped[Commission] = relationship(
        back_populates="photos"
    )
    uploaded_by: Mapped[User | None] = relationship(
        foreign_keys=[uploaded_by_user_id]
    )


class CommissionAttachment(db.Model):
    __tablename__ = "commission_attachment"
    __table_args__ = (
        UniqueConstraint(
            "commission_id",
            "external_file_guid",
            name="uq_commission_attachment_external_file",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=False,
        index=True,
    )
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    source: Mapped[str] = mapped_column(
        db.String(40),
        default="app",
        server_default="app",
        nullable=False,
        index=True,
    )
    source_entity_name: Mapped[str | None] = mapped_column(
        db.String(80),
        index=True,
    )
    source_entity_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(
        db.String(160),
        index=True,
    )
    external_file_guid: Mapped[str | None] = mapped_column(
        db.String(160),
        index=True,
    )
    external_document_id: Mapped[str] = mapped_column(
        db.String(80),
        default=lambda: str(uuid4()),
        nullable=False,
        unique=True,
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
    )
    storage_name: Mapped[str] = mapped_column(
        db.String(500),
        nullable=False,
        unique=True,
    )
    thumbnail_storage_name: Mapped[str | None] = mapped_column(
        db.String(500),
        unique=True,
    )
    mime_type: Mapped[str] = mapped_column(
        db.String(160),
        default="application/octet-stream",
        server_default="application/octet-stream",
        nullable=False,
    )
    size_bytes: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(
        db.String(64),
        nullable=False,
        index=True,
    )
    file_type_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    file_type_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    privacy_category_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    privacy_category_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    caption: Mapped[str | None] = mapped_column(
        db.String(500)
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    erp_sync_state: Mapped[str] = mapped_column(
        db.String(30),
        default=CommissionSyncState.PENDING.value,
        server_default=CommissionSyncState.PENDING.value,
        nullable=False,
        index=True,
    )
    erp_sync_message: Mapped[str | None] = mapped_column(
        db.Text
    )
    erp_synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    external_created_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    external_updated_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    commission: Mapped[Commission] = relationship(
        back_populates="attachments"
    )
    uploaded_by: Mapped[User | None] = relationship(
        foreign_keys=[uploaded_by_user_id]
    )


class AppointmentWindow(db.Model):
    __tablename__ = "appointment_window"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    code: Mapped[str] = mapped_column(
        db.String(80),
        unique=True,
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        index=True,
    )
    start_time: Mapped[time] = mapped_column(
        db.Time,
        nullable=False,
    )
    end_time: Mapped[time] = mapped_column(
        db.Time,
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class CommissionAppointmentRequest(db.Model):
    __tablename__ = "commission_appointment_request"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    recipient_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    recipient_email: Mapped[str] = mapped_column(
        db.String(320),
        nullable=False,
        index=True,
    )
    message: Mapped[str | None] = mapped_column(
        db.Text
    )
    token_hash: Mapped[str] = mapped_column(
        db.String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        db.String(40),
        default=AppointmentRequestStatus.PENDING.value,
        server_default=AppointmentRequestStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    tenant_comment: Mapped[str | None] = mapped_column(
        db.Text
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    expires_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    commission: Mapped[Commission] = relationship(
        back_populates="appointment_requests"
    )
    created_by: Mapped[User | None] = relationship(
        foreign_keys=[created_by_user_id]
    )
    proposals: Mapped[list["CommissionAppointmentProposal"]] = relationship(
        back_populates="appointment_request",
        cascade="all, delete-orphan",
        order_by="CommissionAppointmentProposal.proposed_date",
    )


class CommissionAppointmentProposal(db.Model):
    __tablename__ = "commission_appointment_proposal"
    __table_args__ = (
        UniqueConstraint(
            "appointment_request_id",
            "proposed_date",
            "start_time",
            "end_time",
            name="uq_appointment_proposal_time",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    appointment_request_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "commission_appointment_request.id"
        ),
        nullable=False,
        index=True,
    )
    window_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("appointment_window.id"),
        index=True,
    )
    proposed_date: Mapped[date] = mapped_column(
        db.Date,
        nullable=False,
        index=True,
    )
    start_time: Mapped[time] = mapped_column(
        db.Time,
        nullable=False,
    )
    end_time: Mapped[time] = mapped_column(
        db.Time,
        nullable=False,
    )
    window_name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
    )
    accepted: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default="0",
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    appointment_request: Mapped[CommissionAppointmentRequest] = relationship(
        back_populates="proposals"
    )
    window: Mapped[AppointmentWindow | None] = relationship()


class WowiCompanyCode(db.Model):
    __tablename__ = "wowi_company_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(
        db.Integer,
        unique=True,
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(
        db.String(40),
        unique=True,
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class WowiCommissionType(db.Model):
    __tablename__ = "wowi_commission_type"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(db.Integer, unique=True, nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(db.String(160), index=True)
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class ErpOutboxOperation(db.Model):
    __tablename__ = "erp_outbox_operation"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_erp_outbox_operation_idempotency_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=False,
        index=True,
    )
    operation_type: Mapped[str] = mapped_column(db.String(50), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(db.String(180), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(db.JSON, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(
        db.String(30),
        default=ErpOutboxState.PENDING.value,
        nullable=False,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    available_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(db.Text)
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    commission: Mapped[Commission] = relationship(back_populates="erp_operations")


class InventoryLocation(db.Model):
    __tablename__ = "inventory_location"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    code: Mapped[str] = mapped_column(
        db.String(80),
        unique=True,
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        index=True,
    )
    location_type: Mapped[str] = mapped_column(
        db.String(40),
        default=InventoryLocationType.WAREHOUSE.value,
        server_default=InventoryLocationType.WAREHOUSE.value,
        nullable=False,
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default="0",
        nullable=False,
        index=True,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    stocks: Mapped[list["InventoryStock"]] = relationship(
        back_populates="location",
        cascade="all, delete-orphan",
    )
    outgoing_movements: Mapped[list["InventoryMovement"]] = relationship(
        back_populates="source_location",
        foreign_keys="InventoryMovement.source_location_id",
    )
    incoming_movements: Mapped[list["InventoryMovement"]] = relationship(
        back_populates="target_location",
        foreign_keys="InventoryMovement.target_location_id",
    )

    @property
    def type_label(self) -> str:
        try:
            return InventoryLocationType(
                self.location_type
            ).label
        except ValueError:
            return self.location_type


class InventoryStock(db.Model):
    __tablename__ = "inventory_stock"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "location_id",
            name="uq_inventory_stock_item_location",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        db.ForeignKey("inventory_item.id"),
        nullable=False,
        index=True,
    )
    location_id: Mapped[int] = mapped_column(
        db.ForeignKey("inventory_location.id"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[float] = mapped_column(
        db.Float,
        default=0,
        server_default="0",
        nullable=False,
    )

    item: Mapped[InventoryItem] = relationship(
        back_populates="stocks"
    )
    location: Mapped[InventoryLocation] = relationship(
        back_populates="stocks"
    )


class InventoryItemBarcode(db.Model):
    __tablename__ = "inventory_item_barcode"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        db.ForeignKey("inventory_item.id"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(
        db.String(160),
        unique=True,
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        db.String(40),
        default="ean",
        server_default="ean",
        nullable=False,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default="1",
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    item: Mapped[InventoryItem] = relationship(
        back_populates="barcodes"
    )


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
    stocks: Mapped[list["InventoryStock"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
    )
    barcodes: Mapped[list["InventoryItemBarcode"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
    )
    expected_task_materials: Mapped[list["TimeTaskExpectedMaterial"]] = relationship(back_populates="item")

    @property
    def is_below_reorder_level(self) -> bool:
        if self.reorder_level <= 0:
            return False

        return self.stock_quantity < self.reorder_level


class InventoryMovement(db.Model):
    __tablename__ = "inventory_movement"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(db.ForeignKey("inventory_item.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    commission_id: Mapped[int | None] = mapped_column(db.ForeignKey("commission.id"), index=True)
    task_template_id: Mapped[int | None] = mapped_column(db.ForeignKey("time_task_template.id"), index=True)
    source_location_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("inventory_location.id"),
        index=True,
    )
    target_location_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("inventory_location.id"),
        index=True,
    )
    movement_type: Mapped[str] = mapped_column(db.String(40), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(db.Float, nullable=False)
    note: Mapped[str | None] = mapped_column(db.Text)
    usage_status: Mapped[str] = mapped_column(db.String(40), default=MaterialUsageStatus.INSTALLED.value, server_default=MaterialUsageStatus.INSTALLED.value, nullable=False, index=True)
    source: Mapped[str] = mapped_column(db.String(40), default=MaterialSource.WAREHOUSE.value, server_default=MaterialSource.WAREHOUSE.value, nullable=False, index=True)
    deviation_reason: Mapped[str | None] = mapped_column(db.String(120))
    review_status: Mapped[str] = mapped_column(db.String(40), default=TimeReviewStatus.OK.value, server_default=TimeReviewStatus.OK.value, nullable=False, index=True)
    review_level: Mapped[str] = mapped_column(db.String(20), default=ReviewLevel.GREEN.value, server_default=ReviewLevel.GREEN.value, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    item: Mapped[InventoryItem] = relationship(back_populates="movements")
    user: Mapped[User | None] = relationship(back_populates="inventory_movements", foreign_keys=[user_id])
    commission: Mapped[Commission | None] = relationship(back_populates="inventory_movements")
    task_template: Mapped[TimeTaskTemplate | None] = relationship()
    source_location: Mapped[InventoryLocation | None] = relationship(
        back_populates="outgoing_movements",
        foreign_keys=[source_location_id],
    )
    target_location: Mapped[InventoryLocation | None] = relationship(
        back_populates="incoming_movements",
        foreign_keys=[target_location_id],
    )
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_user_id])


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
    expected_materials: Mapped[list["TimeTaskExpectedMaterial"]] = relationship(back_populates="task_template", cascade="all, delete-orphan")


class TimeTaskExpectedMaterial(db.Model):
    __tablename__ = "time_task_expected_material"
    __table_args__ = (UniqueConstraint("task_template_id", "item_id", name="uq_time_task_expected_material_task_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_template_id: Mapped[int] = mapped_column(db.ForeignKey("time_task_template.id"), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(db.ForeignKey("inventory_item.id"), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(db.Float, default=1, server_default="1", nullable=False)
    required: Mapped[bool] = mapped_column(db.Boolean, default=True, server_default="1", nullable=False)
    note: Mapped[str | None] = mapped_column(db.Text)

    task_template: Mapped[TimeTaskTemplate] = relationship(back_populates="expected_materials")
    item: Mapped[InventoryItem] = relationship(back_populates="expected_task_materials")


class TimeEntry(db.Model):
    __tablename__ = "time_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(db.ForeignKey("commission.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(db.ForeignKey("user_account.id"), nullable=False, index=True)
    task_template_id: Mapped[int | None] = mapped_column(db.ForeignKey("time_task_template.id"), index=True)
    started_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    minutes: Mapped[int] = mapped_column(db.Integer, nullable=False)
    work_minutes: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", nullable=False)
    waiting_minutes: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", nullable=False)
    material_minutes: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", nullable=False)
    travel_minutes: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", nullable=False)
    documentation_minutes: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", nullable=False)
    extra_minutes: Mapped[int] = mapped_column(db.Integer, default=0, server_default="0", nullable=False)
    deviation_reason: Mapped[str | None] = mapped_column(db.String(120))
    note: Mapped[str | None] = mapped_column(db.Text)
    deviation_minutes: Mapped[int] = mapped_column(db.Integer, default=0, nullable=False)
    deviation_percent: Mapped[float] = mapped_column(db.Float, default=0, nullable=False)
    review_status: Mapped[str] = mapped_column(db.String(40), default=TimeReviewStatus.OK.value, nullable=False, index=True)
    review_level: Mapped[str] = mapped_column(db.String(20), default=ReviewLevel.GREEN.value, server_default=ReviewLevel.GREEN.value, nullable=False, index=True)
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

    attempt_count: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    processing_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
        index=True,
    )
    processed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))


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


class WowiBuilding(db.Model):
    __tablename__ = "wowi_building"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(db.Integer, unique=True, index=True, nullable=False)
    id_num: Mapped[str | None] = mapped_column(db.String(100), index=True)
    street: Mapped[str | None] = mapped_column(db.String(255))
    zip_code: Mapped[str | None] = mapped_column(db.String(20))
    town: Mapped[str | None] = mapped_column(db.String(255))
    address: Mapped[str | None] = mapped_column(db.String(500))
    latitude: Mapped[float | None] = mapped_column(db.Float)
    longitude: Mapped[float | None] = mapped_column(db.Float)
    coordinate_source: Mapped[str | None] = mapped_column(db.String(40))
    geocoded_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    exit_date: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))


class WowiUseUnit(db.Model):
    __tablename__ = "wowi_use_unit"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(db.Integer, unique=True, index=True, nullable=False)
    id_num: Mapped[str | None] = mapped_column(db.String(100), index=True)
    building_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    building_id: Mapped[int | None] = mapped_column(db.ForeignKey("wowi_building.id"), index=True)
    floor_name: Mapped[str | None] = mapped_column(db.String(255))
    position_name: Mapped[str | None] = mapped_column(db.String(255))
    description_of_position: Mapped[str | None] = mapped_column(db.String(255))
    address: Mapped[str | None] = mapped_column(db.String(500))
    synced_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    building: Mapped[WowiBuilding | None] = relationship()