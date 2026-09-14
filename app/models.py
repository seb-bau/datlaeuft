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
    APPOINTMENT_ACCEPTED = "appointment_accepted"
    APPOINTMENT_REJECTED = "appointment_rejected"
    NEW_SAFETY_INSPECTION = (
        "new_safety_inspection"
    )
    CHANGED_SAFETY_INSPECTION = (
        "changed_safety_inspection"
    )
    URGENT_SAFETY_DEFECT = (
        "urgent_safety_defect"
    )
    PING = "ping"
    COMMISSION_PRIORITY_RAISED = "commission_priority_raised"


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


class InvoiceBillingRuleType(str, Enum):
    TIME_TYPE = "time_type"
    TASK_TEMPLATE = "task_template"


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"
    CANCELED = "canceled"


class InvoiceArchiveStatus(str, Enum):
    NOT_ARCHIVED = "not_archived"
    FAILED = "failed"
    ARCHIVED = "archived"


class InvoiceDocumentType(str, Enum):
    INVOICE = "invoice"
    CANCELLATION = "cancellation"


class InvoiceRecipientType(str, Enum):
    PRIVATE = "private"
    BUSINESS = "business"


class InvoicePositionSource(str, Enum):
    TIME = "time"
    MATERIAL = "material"
    CATALOG = "catalog"
    FREE = "free"
    CANCELLATION = "cancellation"


class InvoiceKind(str, Enum):
    INVOICE = "invoice"
    CANCELLATION = "cancellation"


class OfferStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_RESPONSE = "awaiting_response"
    IN_PROGRESS = "in_progress"
    READY_FOR_INVOICE = "ready_for_invoice"
    REVIEW_REQUIRED = "review_required"
    INVOICED = "invoiced"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELED = "canceled"


class OfferPositionSource(str, Enum):
    CATALOG = "catalog"
    FREE = "free"


class OfferArchiveStatus(str, Enum):
    NOT_ARCHIVED = "not_archived"
    FAILED = "failed"
    ARCHIVED = "archived"


user_craft_activity = db.Table(
    "user_craft_activity",
    db.Column("user_id", db.ForeignKey("user_account.id"), primary_key=True),
    db.Column("craft_activity_id", db.ForeignKey("craft_activity.id"), primary_key=True),
)


class SafetyInspectionTemplateVersionStatus(
    str,
    Enum,
):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class SafetyInspectionScheduleFrequency(
    str,
    Enum,
):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class SafetyInspectionGenerationMode(
    str,
    Enum,
):
    LEAD_DAYS = "lead_days"
    PREVIOUS_MONTH_DAY = "previous_month_day"


class SafetyInspectionObjectLevel(
    str,
    Enum,
):
    ECONOMIC_UNIT = "economic_unit"
    BUILDING = "building"
    USE_UNIT = "use_unit"


class SafetyInspectionSelectionMode(
    str,
    Enum,
):
    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"


class SafetyInspectionRegexAction(
    str,
    Enum,
):
    INCLUDE = "include"
    EXCLUDE = "exclude"


class SafetyInspectionJobStatus(
    str,
    Enum,
):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_DEFECTS = (
        "completed_with_defects"
    )
    MISSED = "missed"
    CANCELED = "canceled"


class SafetyInspectionResultState(
    str,
    Enum,
):
    NOT_CHECKED = "not_checked"
    NO_DEFECT = "no_defect"
    NOT_PRESENT = "not_present"
    DEFECT_INFO = "defect_info"
    DEFECT_URGENT = "defect_urgent"


class SafetyInspectionDefectStatus(
    str,
    Enum,
):
    OPEN = "open"
    COMMISSION_CREATED = "commission_created"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    REVIEW_REQUIRED = "review_required"
    DISMISSED = "dismissed"


class SafetyInspectionArchiveStatus(
    str,
    Enum,
):
    NOT_ARCHIVED = "not_archived"
    FAILED = "failed"
    ARCHIVED = "archived"


class SafetyInspectionPhotoSource(
    str,
    Enum,
):
    CAMERA = "camera"
    GALLERY = "gallery"
    WEB = "web"


class SafetyInspectionAssignmentSource(str, Enum):
    JURISDICTION = "jurisdiction"
    FALLBACK = "fallback"
    UNASSIGNED = "unassigned"
    MANUAL = "manual"


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
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, onupdate=utc_now,
                                                 nullable=False)


class CraftActivity(db.Model):
    __tablename__ = "craft_activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int | None] = mapped_column(db.Integer, unique=True, index=True)
    code: Mapped[str | None] = mapped_column(db.String(100), index=True)
    name: Mapped[str] = mapped_column(db.String(255), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    app_enabled: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    users: Mapped[list["User"]] = relationship(
        "User",
        secondary=user_craft_activity,
        back_populates="craft_activities",
    )

    service_positions: Mapped[list["TimeTaskTemplate"]] = relationship(back_populates="craft_activity")
    department_mapping: Mapped[
        "CraftActivityDepartmentMapping | None"
    ] = relationship(
        back_populates="craft_activity",
        cascade="all, delete-orphan",
        uselist=False,
    )


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
    can_manage_invoices: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    can_manage_offers: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    can_manage_safety_inspections: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    can_review_safety_inspections: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    push_safety_inspection_enabled: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
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
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="user",
                                                                          foreign_keys="InventoryMovement.user_id")
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
    jurisdiction_person_mappings: Mapped[
        list["UserJurisdictionPersonMapping"]
    ] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
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


class UserJurisdictionPersonMapping(db.Model):
    __tablename__ = (
        "user_jurisdiction_person_mapping"
    )
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction_person_erp_id",
            name=(
                "uq_user_jurisdiction_person_"
                "mapping_person"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    jurisdiction_person_erp_id: Mapped[
        int
    ] = mapped_column(
        db.Integer,
        nullable=False,
    )
    person_name_snapshot: Mapped[
        str | None
    ] = mapped_column(
        db.String(255)
    )
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

    user: Mapped[User] = relationship(
        back_populates=(
            "jurisdiction_person_mappings"
        )
    )


class Commission(db.Model):
    __tablename__ = "commission"

    is_high_priority: Mapped[bool] = mapped_column(
        db.Boolean, default=False, server_default=false(), nullable=False,
    )
    priority_is_manual: Mapped[bool] = mapped_column(
        db.Boolean, default=False, server_default=false(), nullable=False,
    )

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
    economic_unit_erp_id: Mapped[int | None] = mapped_column(
            db.Integer,
            index = True,
    )
    economic_unit_idnum: Mapped[str | None] = mapped_column(
            db.String(100),
            index = True,
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
    invoice_links: Mapped[
        list["InvoiceCommission"]
    ] = relationship(
        back_populates="commission"
    )
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


class CommissionTicketLink(db.Model):
    __tablename__ = "commission_ticket_link"

    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticket_erp_id: Mapped[int] = mapped_column(
        db.Integer, primary_key=True, index=True,
    )


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
    safety_inspection_job_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey(
            "safety_inspection_job.id"
        ),
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
    safety_inspection_job: Mapped[
        "SafetyInspectionJob | None"
    ] = relationship(
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
    commission_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=True,
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
    external_file_guid: Mapped[str | None] = mapped_column(
        db.String(64),
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
    metadata_signature: Mapped[str | None] = mapped_column(
        db.String(64),
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

    commission: Mapped[Commission | None] = relationship(
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
    invoice_number_mask: Mapped[str] = mapped_column(
        db.String(160),
        default="<company_code>-<year>-<counter>",
        server_default="<company_code>-<year>-<counter>",
        nullable=False,
    )
    invoice_counter: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    invoice_counter_year: Mapped[int | None] = mapped_column(
        db.Integer
    )
    invoice_counter_digits: Mapped[int] = mapped_column(
        db.Integer,
        default=5,
        server_default="5",
        nullable=False,
    )
    offer_number_mask: Mapped[str] = mapped_column(
        db.String(160),
        default=(
            "<company_code>-ANG-"
            "<year>-<counter>"
        ),
        server_default=(
            "<company_code>-ANG-"
            "<year>-<counter>"
        ),
        nullable=False,
    )
    offer_counter: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    offer_counter_year: Mapped[int | None] = (
        mapped_column(
            db.Integer
        )
    )
    offer_counter_digits: Mapped[int] = (
        mapped_column(
            db.Integer,
            default=5,
            server_default="5",
            nullable=False,
        )
    )
    stationery_filename: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    invoice_default_hourly_rate_net: Mapped[float | None] = mapped_column(
        db.Numeric(12, 4)
    )
    invoice_default_vat_rate: Mapped[float | None] = mapped_column(
        db.Numeric(5, 2)
    )
    invoice_material_markup_percent: Mapped[float | None] = mapped_column(
        db.Numeric(7, 2)
    )
    invoice_payment_terms_days: Mapped[int | None] = mapped_column(
        db.Integer
    )
    seller_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_street: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_postal_code: Mapped[str | None] = mapped_column(
        db.String(20)
    )
    seller_town: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_country_code: Mapped[str] = mapped_column(
        db.String(2),
        default="DE",
        server_default="DE",
        nullable=False,
    )
    seller_vat_id: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_tax_number: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_email: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_phone: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_iban: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_bic: Mapped[str | None] = mapped_column(
        db.String(40)
    )

    invoice_billing_rules: Mapped[
        list["InvoiceBillingRule"]
    ] = relationship(
        back_populates="company_code",
        cascade="all, delete-orphan",
    )

    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="company_code"
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
    qr_code_payload: Mapped[str] = mapped_column(db.String(160), unique=True, nullable=False,
                                                 default=lambda: f"datlaeuft:item:{uuid4()}")
    stock_quantity: Mapped[float] = mapped_column(db.Float, default=0, nullable=False)
    reorder_level: Mapped[float] = mapped_column(db.Float, default=0, nullable=False)
    billing_base_price_net: Mapped[float | None] = mapped_column(
        db.Numeric(12, 4)
    )
    billing_vat_rate: Mapped[float | None] = mapped_column(
        db.Numeric(5, 2)
    )
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
    usage_status: Mapped[str] = mapped_column(db.String(40), default=MaterialUsageStatus.INSTALLED.value,
                                              server_default=MaterialUsageStatus.INSTALLED.value, nullable=False,
                                              index=True)
    source: Mapped[str] = mapped_column(db.String(40), default=MaterialSource.WAREHOUSE.value,
                                        server_default=MaterialSource.WAREHOUSE.value, nullable=False, index=True)
    deviation_reason: Mapped[str | None] = mapped_column(db.String(120))
    review_status: Mapped[str] = mapped_column(db.String(40), default=TimeReviewStatus.OK.value,
                                               server_default=TimeReviewStatus.OK.value, nullable=False, index=True)
    review_level: Mapped[str] = mapped_column(db.String(20), default=ReviewLevel.GREEN.value,
                                              server_default=ReviewLevel.GREEN.value, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False,
                                                  index=True)
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
    expected_materials: Mapped[list["TimeTaskExpectedMaterial"]] = relationship(back_populates="task_template",
                                                                                cascade="all, delete-orphan")
    invoice_billing_rules: Mapped[
        list["InvoiceBillingRule"]
    ] = relationship(
        back_populates="task_template"
    )


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
    service_date: Mapped[date] = mapped_column(
        db.Date,
        default=date.today,
        nullable=False,
        index=True,
    )
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
    review_status: Mapped[str] = mapped_column(db.String(40), default=TimeReviewStatus.OK.value, nullable=False,
                                               index=True)
    review_level: Mapped[str] = mapped_column(db.String(20), default=ReviewLevel.GREEN.value,
                                              server_default=ReviewLevel.GREEN.value, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user_account.id"), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    commission: Mapped[Commission] = relationship(back_populates="time_entries")
    user: Mapped[User] = relationship(foreign_keys=[user_id], back_populates="time_entries")
    task_template: Mapped[TimeTaskTemplate | None] = relationship()
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_user_id])


class InvoiceBillingRule(db.Model):
    __tablename__ = "invoice_billing_rule"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    company_code_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("wowi_company_code.id"),
        index=True,
    )
    rule_type: Mapped[str] = mapped_column(
        db.String(40),
        nullable=False,
        index=True,
    )
    time_type: Mapped[str | None] = mapped_column(
        db.String(80),
        index=True,
    )
    task_template_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("time_task_template.id"),
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    position_title: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    hourly_rate_net: Mapped[float | None] = mapped_column(
        db.Numeric(12, 4)
    )
    vat_rate: Mapped[float | None] = mapped_column(
        db.Numeric(5, 2)
    )
    sort_order: Mapped[int] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
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

    company_code: Mapped[
        WowiCompanyCode | None
    ] = relationship(
        back_populates="invoice_billing_rules"
    )
    task_template: Mapped[
        TimeTaskTemplate | None
    ] = relationship(
        back_populates="invoice_billing_rules"
    )


class Invoice(db.Model):
    __tablename__ = "invoice"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: str(uuid4()),
    )
    status: Mapped[str] = mapped_column(
        db.String(40),
        default=InvoiceStatus.DRAFT.value,
        server_default=InvoiceStatus.DRAFT.value,
        nullable=False,
        index=True,
    )
    invoice_kind: Mapped[str] = mapped_column(
        db.String(30),
        default=InvoiceKind.INVOICE.value,
        server_default=InvoiceKind.INVOICE.value,
        nullable=False,
        index=True,
    )
    original_invoice_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("invoice.id"),
        unique=True,
        index=True,
    )
    document_type: Mapped[str] = mapped_column(
        db.String(40),
        default=InvoiceDocumentType.INVOICE.value,
        server_default=InvoiceDocumentType.INVOICE.value,
        nullable=False,
        index=True,
    )
    dms_archive_status: Mapped[str] = mapped_column(
        db.String(30),
        default=InvoiceArchiveStatus.NOT_ARCHIVED.value,
        server_default=InvoiceArchiveStatus.NOT_ARCHIVED.value,
        nullable=False,
        index=True,
    )
    dms_archive_key: Mapped[str | None] = mapped_column(
        db.String(180),
        unique=True,
        index=True,
    )
    dms_document_id: Mapped[str | None] = mapped_column(
        db.String(255),
        unique=True,
        index=True,
    )
    dms_archive_pdf_sha256: Mapped[str | None] = mapped_column(
        db.String(64),
    )
    dms_archive_attempted_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
    )
    dms_archived_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True),
    )
    dms_archived_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    dms_archive_error: Mapped[str | None] = mapped_column(
        db.Text,
    )

    dms_archived_by: Mapped[User | None] = relationship(
        foreign_keys=[dms_archived_by_user_id],
    )
    company_code_id: Mapped[int] = mapped_column(
        db.ForeignKey("wowi_company_code.id"),
        nullable=False,
        index=True,
    )
    company_code_erp_id: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
        index=True,
    )
    company_code_code: Mapped[str] = mapped_column(
        db.String(40),
        nullable=False,
        index=True,
    )
    invoice_number: Mapped[str | None] = mapped_column(
        db.String(160),
        unique=True,
        index=True,
    )
    invoice_date: Mapped[date | None] = mapped_column(
        db.Date
    )
    service_date_from: Mapped[date | None] = mapped_column(
        db.Date
    )
    service_date_to: Mapped[date | None] = mapped_column(
        db.Date
    )
    due_date: Mapped[date | None] = mapped_column(
        db.Date
    )
    recipient_type: Mapped[str] = mapped_column(
        db.String(40),
        default=InvoiceRecipientType.PRIVATE.value,
        server_default=InvoiceRecipientType.PRIVATE.value,
        nullable=False,
    )
    recipient_person_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    recipient_license_agreement_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    recipient_license_agreement_idnum: Mapped[str | None] = mapped_column(
        db.String(100),
        index=True,
    )
    recipient_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    recipient_name_addition: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    recipient_street: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    recipient_postal_code: Mapped[str | None] = mapped_column(
        db.String(20)
    )
    recipient_town: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    recipient_country_code: Mapped[str] = mapped_column(
        db.String(2),
        default="DE",
        server_default="DE",
        nullable=False,
    )
    recipient_email: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    recipient_vat_id: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_street: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_postal_code: Mapped[str | None] = mapped_column(
        db.String(20)
    )
    seller_town: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_country_code: Mapped[str | None] = mapped_column(
        db.String(2)
    )
    seller_vat_id: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_tax_number: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_email: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    seller_phone: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_iban: Mapped[str | None] = mapped_column(
        db.String(80)
    )
    seller_bic: Mapped[str | None] = mapped_column(
        db.String(40)
    )
    currency_code: Mapped[str] = mapped_column(
        db.String(3),
        default="EUR",
        server_default="EUR",
        nullable=False,
    )
    payment_reference: Mapped[str | None] = mapped_column(
        db.String(160)
    )
    introductory_text: Mapped[str | None] = mapped_column(
        db.Text
    )
    concluding_text: Mapped[str | None] = mapped_column(
        db.Text
    )
    total_net: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    total_vat: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    total_gross: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    document_filename: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    document_sha256: Mapped[str | None] = mapped_column(
        db.String(64)
    )
    dvelop_document_id: Mapped[str | None] = mapped_column(
        db.String(255),
        index=True,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    archive_error: Mapped[str | None] = mapped_column(
        db.Text
    )
    reference_invoice_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("invoice.id"),
        index=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(
        db.Text
    )
    created_by_user_id: Mapped[int] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    finalized_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    canceled_by_user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("user_account.id"),
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
    finalized_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    company_code: Mapped[WowiCompanyCode] = relationship(
        back_populates="invoices"
    )
    positions: Mapped[list["InvoicePosition"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoicePosition.position_index",
    )
    commission_links: Mapped[
        list["InvoiceCommission"]
    ] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )
    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    finalized_by: Mapped[User | None] = relationship(
        foreign_keys=[finalized_by_user_id]
    )
    canceled_by: Mapped[User | None] = relationship(
        foreign_keys=[canceled_by_user_id]
    )
    reference_invoice: Mapped["Invoice | None"] = relationship(
        "Invoice",
        remote_side="Invoice.id",
        foreign_keys=[reference_invoice_id],
        back_populates="related_documents",
    )
    related_documents: Mapped[list["Invoice"]] = relationship(
        "Invoice",
        foreign_keys="Invoice.reference_invoice_id",
        back_populates="reference_invoice",
    )
    original_invoice: Mapped["Invoice | None"] = relationship(
        "Invoice",
        remote_side="Invoice.id",
        foreign_keys=[original_invoice_id],
        back_populates="cancellation_invoice",
    )
    cancellation_invoice: Mapped["Invoice | None"] = relationship(
        "Invoice",
        foreign_keys="Invoice.original_invoice_id",
        back_populates="original_invoice",
        uselist=False,
    )


class InvoicePosition(db.Model):
    __tablename__ = "invoice_position"
    __table_args__ = (
        UniqueConstraint(
            "invoice_id",
            "position_index",
            name="uq_invoice_position_index",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    invoice_id: Mapped[int] = mapped_column(
        db.ForeignKey("invoice.id"),
        nullable=False,
        index=True,
    )
    position_index: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        db.String(40),
        default=InvoicePositionSource.FREE.value,
        server_default=InvoicePositionSource.FREE.value,
        nullable=False,
        index=True,
    )
    task_template_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("time_task_template.id"),
        index=True,
    )
    inventory_item_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("inventory_item.id"),
        index=True,
    )
    source_data: Mapped[dict[str, Any] | None] = mapped_column(
        db.JSON
    )
    description: Mapped[str] = mapped_column(
        db.Text,
        nullable=False,
    )
    details: Mapped[str | None] = mapped_column(
        db.Text
    )
    quantity: Mapped[float] = mapped_column(
        db.Numeric(12, 4),
        default=1,
        server_default="1",
        nullable=False,
    )
    unit: Mapped[str] = mapped_column(
        db.String(40),
        default="Stk",
        server_default="Stk",
        nullable=False,
    )
    unit_price_net: Mapped[float] = mapped_column(
        db.Numeric(14, 4),
        default=0,
        server_default="0",
        nullable=False,
    )
    vat_rate: Mapped[float] = mapped_column(
        db.Numeric(5, 2),
        default=19,
        server_default="19",
        nullable=False,
    )
    net_amount: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    vat_amount: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    gross_amount: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
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

    invoice: Mapped[Invoice] = relationship(
        back_populates="positions"
    )
    task_template: Mapped[
        TimeTaskTemplate | None
    ] = relationship()
    inventory_item: Mapped[
        InventoryItem | None
    ] = relationship()


class InvoiceCommission(db.Model):
    __tablename__ = "invoice_commission"
    __table_args__ = (
        UniqueConstraint(
            "invoice_id",
            "commission_id",
            name="uq_invoice_commission",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    invoice_id: Mapped[int] = mapped_column(
        db.ForeignKey("invoice.id"),
        nullable=False,
        index=True,
    )
    commission_id: Mapped[int] = mapped_column(
        db.ForeignKey("commission.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    invoice: Mapped[Invoice] = relationship(
        back_populates="commission_links"
    )
    commission: Mapped[Commission] = relationship(
        back_populates="invoice_links"
    )


class Offer(db.Model):
    __tablename__ = "offer"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: str(uuid4()),
    )
    status: Mapped[str] = mapped_column(
        db.String(40),
        default=OfferStatus.DRAFT.value,
        server_default=OfferStatus.DRAFT.value,
        nullable=False,
        index=True,
    )
    source_offer_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("offer.id"),
            index=True,
        )
    )
    company_code_id: Mapped[int] = mapped_column(
        db.ForeignKey("wowi_company_code.id"),
        nullable=False,
        index=True,
    )
    company_code_erp_id: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
            index=True,
        )
    )
    company_code_code: Mapped[str] = (
        mapped_column(
            db.String(40),
            nullable=False,
            index=True,
        )
    )
    craft_activity_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("craft_activity.id"),
            index=True,
        )
    )
    offer_number: Mapped[str | None] = (
        mapped_column(
            db.String(160),
            unique=True,
            index=True,
        )
    )
    offer_date: Mapped[date | None] = (
        mapped_column(
            db.Date
        )
    )
    valid_until: Mapped[date | None] = (
        mapped_column(
            db.Date,
            index=True,
        )
    )
    title: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    object_type: Mapped[str | None] = (
        mapped_column(
            db.String(40),
            index=True,
        )
    )
    object_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    economic_unit_erp_id: Mapped[int | None] = (
        mapped_column(
            db.Integer,
            index=True,
        )
    )
    building_erp_id: Mapped[int | None] = (
        mapped_column(
            db.Integer,
            index=True,
        )
    )
    use_unit_erp_id: Mapped[int | None] = (
        mapped_column(
            db.Integer,
            index=True,
        )
    )
    license_agreement_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    license_agreement_idnum: Mapped[
        str | None
    ] = mapped_column(
        db.String(100),
        index=True,
    )
    object_address: Mapped[str | None] = (
        mapped_column(
            db.String(500)
        )
    )
    object_location: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    tenant_name: Mapped[str | None] = (
        mapped_column(
            db.String(255),
            index=True,
        )
    )
    recipient_type: Mapped[str] = mapped_column(
        db.String(40),
        default=InvoiceRecipientType.PRIVATE.value,
        server_default=(
            InvoiceRecipientType.PRIVATE.value
        ),
        nullable=False,
    )
    recipient_person_erp_id: Mapped[int | None] = (
        mapped_column(
            db.Integer,
            index=True,
        )
    )
    recipient_name: Mapped[str | None] = (
        mapped_column(
            db.String(255),
            index=True,
        )
    )
    recipient_name_addition: Mapped[
        str | None
    ] = mapped_column(
        db.String(255)
    )
    recipient_street: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    recipient_postal_code: Mapped[
        str | None
    ] = mapped_column(
        db.String(20)
    )
    recipient_town: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    recipient_country_code: Mapped[str] = (
        mapped_column(
            db.String(2),
            default="DE",
            server_default="DE",
            nullable=False,
        )
    )
    recipient_email: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    recipient_vat_id: Mapped[str | None] = (
        mapped_column(
            db.String(80)
        )
    )
    seller_name: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    seller_street: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    seller_postal_code: Mapped[str | None] = (
        mapped_column(
            db.String(20)
        )
    )
    seller_town: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    seller_country_code: Mapped[str] = (
        mapped_column(
            db.String(2),
            default="DE",
            server_default="DE",
            nullable=False,
        )
    )
    seller_vat_id: Mapped[str | None] = (
        mapped_column(
            db.String(80)
        )
    )
    seller_tax_number: Mapped[str | None] = (
        mapped_column(
            db.String(80)
        )
    )
    seller_email: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    seller_phone: Mapped[str | None] = (
        mapped_column(
            db.String(80)
        )
    )
    currency_code: Mapped[str] = mapped_column(
        db.String(3),
        default="EUR",
        server_default="EUR",
        nullable=False,
    )
    introductory_text: Mapped[str | None] = (
        mapped_column(
            db.Text
        )
    )
    concluding_text: Mapped[str | None] = (
        mapped_column(
            db.Text
        )
    )
    total_net: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    total_vat: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    total_gross: Mapped[float] = (
        mapped_column(
            db.Numeric(14, 2),
            default=0,
            server_default="0",
            nullable=False,
        )
    )
    document_filename: Mapped[str | None] = (
        mapped_column(
            db.String(255)
        )
    )
    document_sha256: Mapped[str | None] = (
        mapped_column(
            db.String(64)
        )
    )
    commission_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("commission.id"),
            unique=True,
            index=True,
        )
    )
    invoice_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("invoice.id"),
            unique=True,
            index=True,
        )
    )
    status_note: Mapped[str | None] = (
        mapped_column(
            db.Text
        )
    )
    dms_archive_status: Mapped[str] = (
        mapped_column(
            db.String(30),
            default=(
                OfferArchiveStatus
                .NOT_ARCHIVED.value
            ),
            server_default=(
                OfferArchiveStatus
                .NOT_ARCHIVED.value
            ),
            nullable=False,
            index=True,
        )
    )
    dms_archive_key: Mapped[str | None] = (
        mapped_column(
            db.String(180),
            unique=True,
            index=True,
        )
    )
    dms_document_id: Mapped[str | None] = (
        mapped_column(
            db.String(255),
            unique=True,
            index=True,
        )
    )
    dms_archive_pdf_sha256: Mapped[
        str | None
    ] = mapped_column(
        db.String(64)
    )
    dms_archive_attempted_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    dms_archived_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )
    dms_archived_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    dms_archive_error: Mapped[str | None] = (
        mapped_column(
            db.Text
        )
    )
    created_by_user_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            nullable=False,
            index=True,
        )
    )
    released_by_user_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            index=True,
        )
    )
    accepted_by_user_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            index=True,
        )
    )
    rejected_by_user_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            index=True,
        )
    )
    canceled_by_user_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            index=True,
        )
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
    released_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )
    accepted_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )
    rejected_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )
    canceled_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )
    invoiced_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )

    company_code: Mapped[WowiCompanyCode] = (
        relationship()
    )
    craft_activity: Mapped[
        CraftActivity | None
    ] = relationship()
    commission: Mapped[
        Commission | None
    ] = relationship()
    invoice: Mapped[Invoice | None] = relationship()
    positions: Mapped[list["OfferPosition"]] = (
        relationship(
            back_populates="offer",
            cascade="all, delete-orphan",
            order_by="OfferPosition.position_index",
        )
    )
    source_offer: Mapped["Offer | None"] = (
        relationship(
            "Offer",
            remote_side=[id],
            foreign_keys=[source_offer_id],
        )
    )
    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    released_by: Mapped[User | None] = (
        relationship(
            foreign_keys=[released_by_user_id]
        )
    )
    accepted_by: Mapped[User | None] = (
        relationship(
            foreign_keys=[accepted_by_user_id]
        )
    )
    rejected_by: Mapped[User | None] = (
        relationship(
            foreign_keys=[rejected_by_user_id]
        )
    )
    canceled_by: Mapped[User | None] = (
        relationship(
            foreign_keys=[canceled_by_user_id]
        )
    )
    dms_archived_by: Mapped[User | None] = (
        relationship(
            foreign_keys=[
                dms_archived_by_user_id
            ]
        )
    )


class OfferPosition(db.Model):
    __tablename__ = "offer_position"
    __table_args__ = (
        UniqueConstraint(
            "offer_id",
            "position_index",
            name="uq_offer_position_index",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    offer_id: Mapped[int] = mapped_column(
        db.ForeignKey("offer.id"),
        nullable=False,
        index=True,
    )
    position_index: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        db.String(40),
        default=OfferPositionSource.FREE.value,
        server_default=(
            OfferPositionSource.FREE.value
        ),
        nullable=False,
        index=True,
    )
    task_template_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("time_task_template.id"),
            index=True,
        )
    )
    description: Mapped[str] = mapped_column(
        db.Text,
        nullable=False,
    )
    details: Mapped[str | None] = mapped_column(
        db.Text
    )
    quantity: Mapped[float] = mapped_column(
        db.Numeric(12, 4),
        default=1,
        server_default="1",
        nullable=False,
    )
    unit: Mapped[str] = mapped_column(
        db.String(40),
        default="Stk",
        server_default="Stk",
        nullable=False,
    )
    unit_price_net: Mapped[float] = (
        mapped_column(
            db.Numeric(14, 4),
            default=0,
            server_default="0",
            nullable=False,
        )
    )
    vat_rate: Mapped[float] = mapped_column(
        db.Numeric(5, 2),
        default=19,
        server_default="19",
        nullable=False,
    )
    net_amount: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    vat_amount: Mapped[float] = mapped_column(
        db.Numeric(14, 2),
        default=0,
        server_default="0",
        nullable=False,
    )
    gross_amount: Mapped[float] = (
        mapped_column(
            db.Numeric(14, 2),
            default=0,
            server_default="0",
            nullable=False,
        )
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

    offer: Mapped[Offer] = relationship(
        back_populates="positions"
    )
    task_template: Mapped[
        TimeTaskTemplate | None
    ] = relationship()


class OfferCraftFallback(db.Model):
    __tablename__ = "offer_craft_fallback"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    craft_activity_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey("craft_activity.id"),
            unique=True,
            nullable=False,
            index=True,
        )
    )
    task_template_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey("time_task_template.id"),
            unique=True,
            nullable=False,
            index=True,
        )
    )
    updated_by_user_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            index=True,
        )
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    craft_activity: Mapped[CraftActivity] = (
        relationship()
    )
    task_template: Mapped[TimeTaskTemplate] = (
        relationship()
    )
    updated_by: Mapped[User | None] = (
        relationship()
    )


class SafetyInspectionTemplate(db.Model):
    __tablename__ = "safety_inspection_template"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        index=True,
    )
    description: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    legal_reference: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
        )
    )
    updated_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            onupdate=utc_now,
            nullable=False,
        )
    )

    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    versions: Mapped[
        list["SafetyInspectionTemplateVersion"]
    ] = relationship(
        back_populates="template",
        order_by=(
            "SafetyInspectionTemplateVersion."
            "version_number"
        ),
    )
    groups: Mapped[
        list["SafetyInspectionGroup"]
    ] = relationship(
        back_populates="template"
    )


class SafetyInspectionTemplateVersion(db.Model):
    __tablename__ = (
        "safety_inspection_template_version"
    )
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "version_number",
            name=(
                "uq_safety_inspection_template_"
                "version"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    template_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_template.id"
        ),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
        )
    )
    status: Mapped[str] = mapped_column(
        db.String(30),
        default=(
            SafetyInspectionTemplateVersionStatus
            .DRAFT.value
        ),
        server_default=(
            SafetyInspectionTemplateVersionStatus
            .DRAFT.value
        ),
        nullable=False,
        index=True,
    )
    change_note: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    created_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    published_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
        )
    )
    published_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )

    template: Mapped[
        SafetyInspectionTemplate
    ] = relationship(
        back_populates="versions"
    )
    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    published_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[published_by_user_id]
    )
    items: Mapped[
        list["SafetyInspectionTemplateItem"]
    ] = relationship(
        back_populates="template_version",
        cascade="all, delete-orphan",
        order_by=(
            "SafetyInspectionTemplateItem."
            "position_index"
        ),
    )
    jobs: Mapped[
        list["SafetyInspectionJob"]
    ] = relationship(
        back_populates="template_version"
    )


class SafetyInspectionTemplateItem(db.Model):
    __tablename__ = (
        "safety_inspection_template_item"
    )
    __table_args__ = (
        UniqueConstraint(
            "template_version_id",
            "position_index",
            name=(
                "uq_safety_inspection_template_"
                "item_position"
            ),
        ),
        UniqueConstraint(
            "template_version_id",
            "stable_key",
            name=(
                "uq_safety_inspection_template_"
                "item_key"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    template_version_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey(
            "safety_inspection_template_version.id"
        ),
        nullable=False,
        index=True,
    )
    stable_key: Mapped[str] = mapped_column(
        db.String(36),
        default=lambda: str(uuid4()),
        nullable=False,
    )
    position_index: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
        )
    )
    title: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
    )
    description: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    help_text: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    required: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    allow_not_present: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    photo_required_no_defect: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    photo_required_not_present: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    photo_required_defect_info: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    photo_required_defect_urgent: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )

    template_version: Mapped[
        SafetyInspectionTemplateVersion
    ] = relationship(
        back_populates="items"
    )


class SafetyInspectionSchedule(db.Model):
    __tablename__ = "safety_inspection_schedule"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    frequency: Mapped[str] = mapped_column(
        db.String(30),
        default=(
            SafetyInspectionScheduleFrequency
            .MONTHLY.value
        ),
        server_default=(
            SafetyInspectionScheduleFrequency
            .MONTHLY.value
        ),
        nullable=False,
    )
    interval_value: Mapped[int] = (
        mapped_column(
            db.Integer,
            default=1,
            server_default="1",
            nullable=False,
        )
    )
    generation_mode: Mapped[
        str
    ] = mapped_column(
        db.String(40),
        default=(
            SafetyInspectionGenerationMode
            .PREVIOUS_MONTH_DAY.value
        ),
        server_default=(
            SafetyInspectionGenerationMode
            .PREVIOUS_MONTH_DAY.value
        ),
        nullable=False,
    )
    generation_lead_days: Mapped[
        int
    ] = mapped_column(
        db.Integer,
        default=7,
        server_default="7",
        nullable=False,
    )
    generation_day_of_month: Mapped[
        int | None
    ] = mapped_column(db.Integer)
    due_start_offset_days: Mapped[
        int
    ] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    due_end_offset_days: Mapped[
        int
    ] = mapped_column(
        db.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    warning_days: Mapped[int] = (
        mapped_column(
            db.Integer,
            default=7,
            server_default="7",
            nullable=False,
        )
    )
    grace_days: Mapped[int] = (
        mapped_column(
            db.Integer,
            default=0,
            server_default="0",
            nullable=False,
        )
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
        )
    )
    updated_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            onupdate=utc_now,
            nullable=False,
        )
    )

    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    groups: Mapped[
        list["SafetyInspectionGroup"]
    ] = relationship(
        back_populates="schedule"
    )


class SafetyInspectionGroup(db.Model):
    __tablename__ = "safety_inspection_group"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    description: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    template_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_template.id"
        ),
        nullable=False,
        index=True,
    )
    schedule_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_schedule.id"
        ),
        nullable=False,
        index=True,
    )
    object_level: Mapped[str] = (
        mapped_column(
            db.String(30),
            nullable=False,
            index=True,
        )
    )
    selection_mode: Mapped[str] = (
        mapped_column(
            db.String(30),
            default=(
                SafetyInspectionSelectionMode
                .WHITELIST.value
            ),
            server_default=(
                SafetyInspectionSelectionMode
                .WHITELIST.value
            ),
            nullable=False,
        )
    )
    id_num_regex: Mapped[
        str | None
    ] = mapped_column(db.Text)
    id_num_regex_action: Mapped[
        str | None
    ] = mapped_column(
        db.String(20)
    )
    department_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey("wowi_department.id"),
            nullable=False,
            index=True,
        )
    )
    fallback_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    corrective_commission_type_erp_id: Mapped[
        int
    ] = mapped_column(
        db.Integer,
        nullable=False,
        index=True,
    )
    corrective_craft_activity_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("craft_activity.id"),
        nullable=False,
        index=True,
    )
    corrective_task_template_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey(
            "time_task_template.id"
        ),
        nullable=False,
        index=True,
    )
    first_period_start: Mapped[
        date
    ] = mapped_column(
        db.Date,
        nullable=False,
        index=True,
    )
    ends_on: Mapped[
        date | None
    ] = mapped_column(
        db.Date,
        index=True,
    )
    include_vacancies: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    automatic_archive: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    archive_original_photos: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )
    last_generation_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    last_generation_error: Mapped[
        str | None
    ] = mapped_column(db.Text)
    created_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
        )
    )
    updated_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            onupdate=utc_now,
            nullable=False,
        )
    )

    template: Mapped[
        SafetyInspectionTemplate
    ] = relationship(
        back_populates="groups"
    )
    schedule: Mapped[
        SafetyInspectionSchedule
    ] = relationship(
        back_populates="groups"
    )
    department: Mapped[
        WowiDepartment
    ] = relationship()
    fallback_user: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[fallback_user_id]
    )
    corrective_craft_activity: Mapped[
        CraftActivity
    ] = relationship()
    corrective_task_template: Mapped[
        TimeTaskTemplate
    ] = relationship()
    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    targets: Mapped[
        list["SafetyInspectionGroupTarget"]
    ] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[
        list["SafetyInspectionJob"]
    ] = relationship(
        back_populates="group"
    )


class SafetyInspectionGroupTarget(db.Model):
    __tablename__ = (
        "safety_inspection_group_target"
    )
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "object_type",
            "object_erp_id",
            name=(
                "uq_safety_inspection_group_"
                "target"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_group.id"
        ),
        nullable=False,
        index=True,
    )
    object_type: Mapped[str] = (
        mapped_column(
            db.String(30),
            nullable=False,
            index=True,
        )
    )
    object_erp_id: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
            index=True,
        )
    )
    object_label: Mapped[
        str | None
    ] = mapped_column(db.String(500))
    included: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )

    group: Mapped[
        SafetyInspectionGroup
    ] = relationship(
        back_populates="targets"
    )


class SafetyInspectionJob(db.Model):
    __tablename__ = "safety_inspection_job"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "object_level",
            "object_erp_id",
            "period_start",
            name=(
                "uq_safety_inspection_job_period"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        default=lambda: str(uuid4()),
        nullable=False,
        unique=True,
        index=True,
    )
    group_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_group.id"
        ),
        nullable=False,
        index=True,
    )
    template_version_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey(
            "safety_inspection_template_version.id"
        ),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        db.String(40),
        default=(
            SafetyInspectionJobStatus
            .PLANNED.value
        ),
        server_default=(
            SafetyInspectionJobStatus
            .PLANNED.value
        ),
        nullable=False,
        index=True,
    )
    period_key: Mapped[str] = (
        mapped_column(
            db.String(80),
            nullable=False,
            index=True,
        )
    )
    period_start: Mapped[date] = (
        mapped_column(
            db.Date,
            nullable=False,
            index=True,
        )
    )
    period_end: Mapped[date] = (
        mapped_column(
            db.Date,
            nullable=False,
            index=True,
        )
    )
    due_from: Mapped[date] = mapped_column(
        db.Date,
        nullable=False,
        index=True,
    )
    due_to: Mapped[date] = mapped_column(
        db.Date,
        nullable=False,
        index=True,
    )
    warning_on: Mapped[date] = (
        mapped_column(
            db.Date,
            nullable=False,
            index=True,
        )
    )
    missed_on: Mapped[date] = (
        mapped_column(
            db.Date,
            nullable=False,
            index=True,
        )
    )
    object_level: Mapped[str] = (
        mapped_column(
            db.String(30),
            nullable=False,
            index=True,
        )
    )
    object_erp_id: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
            index=True,
        )
    )
    company_code_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    company_code_code: Mapped[
        str | None
    ] = mapped_column(
        db.String(40),
        index=True,
    )
    economic_unit_erp_id: Mapped[
        int
    ] = mapped_column(
        db.Integer,
        nullable=False,
        index=True,
    )
    economic_unit_idnum: Mapped[
        str | None
    ] = mapped_column(
        db.String(100),
        index=True,
    )
    economic_unit_name: Mapped[
        str | None
    ] = mapped_column(db.String(255))
    building_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    building_idnum: Mapped[
        str | None
    ] = mapped_column(
        db.String(100),
        index=True,
    )
    use_unit_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    use_unit_idnum: Mapped[
        str | None
    ] = mapped_column(
        db.String(100),
        index=True,
    )
    object_address: Mapped[
        str | None
    ] = mapped_column(db.String(500))
    object_location: Mapped[
        str | None
    ] = mapped_column(db.String(255))
    latitude: Mapped[
        float | None
    ] = mapped_column(db.Float)
    longitude: Mapped[
        float | None
    ] = mapped_column(db.Float)
    department_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey("wowi_department.id"),
            nullable=False,
            index=True,
        )
    )
    jurisdiction_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey(
            "wowi_economic_unit_jurisdiction.id"
        ),
        index=True,
    )
    assigned_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    responsible_official_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    assignment_source: Mapped[
        str
    ] = mapped_column(
        db.String(30),
        default=(
            SafetyInspectionAssignmentSource
            .UNASSIGNED.value
        ),
        server_default=(
            SafetyInspectionAssignmentSource
            .UNASSIGNED.value
        ),
        nullable=False,
        index=True,
    )
    assignment_error: Mapped[
        str | None
    ] = mapped_column(db.Text)
    overall_note: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    started_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    completed_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    completed_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    missed_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    canceled_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    canceled_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    cancellation_reason: Mapped[
        str | None
    ] = mapped_column(db.Text)
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
            index=True,
        )
    )
    updated_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            onupdate=utc_now,
            nullable=False,
        )
    )

    group: Mapped[
        SafetyInspectionGroup
    ] = relationship(
        back_populates="jobs"
    )
    template_version: Mapped[
        SafetyInspectionTemplateVersion
    ] = relationship(
        back_populates="jobs"
    )
    department: Mapped[
        WowiDepartment
    ] = relationship()
    jurisdiction: Mapped[
        WowiEconomicUnitJurisdiction | None
    ] = relationship()
    assigned_user: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[assigned_user_id]
    )
    completed_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[completed_by_user_id]
    )
    canceled_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[canceled_by_user_id]
    )
    results: Mapped[
        list["SafetyInspectionResult"]
    ] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by=(
            "SafetyInspectionResult."
            "position_index"
        ),
    )
    defects: Mapped[
        list["SafetyInspectionDefect"]
    ] = relationship(
        back_populates="job"
    )
    protocols: Mapped[
        list["SafetyInspectionProtocol"]
    ] = relationship(
        back_populates="job",
        order_by=(
            "SafetyInspectionProtocol."
            "version_number"
        ),
    )
    push_deliveries: Mapped[
        list["PushDelivery"]
    ] = relationship(
        back_populates="safety_inspection_job",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            SafetyInspectionJobStatus
            .COMPLETED.value,
            SafetyInspectionJobStatus
            .COMPLETED_WITH_DEFECTS.value,
            SafetyInspectionJobStatus
            .MISSED.value,
            SafetyInspectionJobStatus
            .CANCELED.value,
        }

    @property
    def is_overdue(self) -> bool:
        return (
            not self.is_terminal
            and self.due_to < date.today()
        )

    @property
    def is_due(self) -> bool:
        return (
            not self.is_terminal
            and self.due_from <= date.today()
            <= self.due_to
        )


class SafetyInspectionResult(db.Model):
    __tablename__ = "safety_inspection_result"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "template_item_id",
            name=(
                "uq_safety_inspection_result_item"
            ),
        ),
        UniqueConstraint(
            "job_id",
            "position_index",
            name=(
                "uq_safety_inspection_result_position"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        default=lambda: str(uuid4()),
        nullable=False,
        unique=True,
        index=True,
    )
    job_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_job.id"
        ),
        nullable=False,
        index=True,
    )
    template_item_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey(
                "safety_inspection_template_item.id"
            ),
            nullable=False,
            index=True,
        )
    )
    item_key: Mapped[str] = mapped_column(
        db.String(36),
        nullable=False,
    )
    position_index: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
        )
    )
    item_title: Mapped[str] = (
        mapped_column(
            db.String(255),
            nullable=False,
        )
    )
    item_description: Mapped[
        str | None
    ] = mapped_column(db.Text)
    item_help_text: Mapped[
        str | None
    ] = mapped_column(db.Text)
    required: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    allow_not_present: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    photo_required_no_defect: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    photo_required_not_present: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    photo_required_defect_info: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    photo_required_defect_urgent: Mapped[
        bool
    ] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        db.String(40),
        default=(
            SafetyInspectionResultState
            .NOT_CHECKED.value
        ),
        server_default=(
            SafetyInspectionResultState
            .NOT_CHECKED.value
        ),
        nullable=False,
        index=True,
    )
    note: Mapped[str | None] = (
        mapped_column(db.Text)
    )
    immediate_action: Mapped[
        str | None
    ] = mapped_column(db.Text)
    checked_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    checked_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    version: Mapped[int] = mapped_column(
        db.Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    updated_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            onupdate=utc_now,
            nullable=False,
        )
    )

    job: Mapped[
        SafetyInspectionJob
    ] = relationship(
        back_populates="results"
    )
    template_item: Mapped[
        SafetyInspectionTemplateItem
    ] = relationship()
    checked_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[checked_by_user_id]
    )
    photos: Mapped[
        list["SafetyInspectionPhoto"]
    ] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
    )
    defect: Mapped[
        "SafetyInspectionDefect | None"
    ] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        uselist=False,
    )


class SafetyInspectionPhoto(db.Model):
    __tablename__ = "safety_inspection_photo"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        default=lambda: str(uuid4()),
        nullable=False,
        unique=True,
        index=True,
    )
    result_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_result.id"
        ),
        nullable=False,
        index=True,
    )
    uploaded_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(
        db.String(30),
        default=(
            SafetyInspectionPhotoSource
            .CAMERA.value
        ),
        server_default=(
            SafetyInspectionPhotoSource
            .CAMERA.value
        ),
        nullable=False,
        index=True,
    )
    original_filename: Mapped[str] = (
        mapped_column(
            db.String(255),
            nullable=False,
        )
    )
    storage_name: Mapped[str] = (
        mapped_column(
            db.String(500),
            nullable=False,
            unique=True,
        )
    )
    thumbnail_storage_name: Mapped[
        str
    ] = mapped_column(
        db.String(500),
        nullable=False,
        unique=True,
    )
    mime_type: Mapped[str] = (
        mapped_column(
            db.String(100),
            default="image/jpeg",
            server_default="image/jpeg",
            nullable=False,
        )
    )
    size_bytes: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
        )
    )
    content_sha256: Mapped[str] = (
        mapped_column(
            db.String(64),
            nullable=False,
            index=True,
        )
    )
    caption: Mapped[str | None] = (
        mapped_column(db.String(500))
    )
    captured_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )
    dms_archive_status: Mapped[
        str
    ] = mapped_column(
        db.String(30),
        default=(
            SafetyInspectionArchiveStatus
            .NOT_ARCHIVED.value
        ),
        server_default=(
            SafetyInspectionArchiveStatus
            .NOT_ARCHIVED.value
        ),
        nullable=False,
        index=True,
    )
    dms_document_id: Mapped[
        str | None
    ] = mapped_column(
        db.String(255),
        unique=True,
    )
    dms_archive_attempted_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    dms_archived_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    dms_archive_error: Mapped[
        str | None
    ] = mapped_column(db.Text)
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
            index=True,
        )
    )
    updated_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            onupdate=utc_now,
            nullable=False,
        )
    )
    deleted_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )

    result: Mapped[
        SafetyInspectionResult
    ] = relationship(
        back_populates="photos"
    )
    uploaded_by: Mapped[User] = relationship(
        foreign_keys=[uploaded_by_user_id]
    )


class SafetyInspectionDefect(db.Model):
    __tablename__ = "safety_inspection_defect"
    __table_args__ = (
        UniqueConstraint(
            "result_id",
            name=(
                "uq_safety_inspection_defect_result"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    public_id: Mapped[str] = mapped_column(
        db.String(36),
        default=lambda: str(uuid4()),
        nullable=False,
        unique=True,
        index=True,
    )
    job_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_job.id"
        ),
        nullable=False,
        index=True,
    )
    result_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_result.id"
        ),
        nullable=False,
        index=True,
    )
    severity: Mapped[str] = (
        mapped_column(
            db.String(40),
            nullable=False,
            index=True,
        )
    )
    status: Mapped[str] = mapped_column(
        db.String(40),
        default=(
            SafetyInspectionDefectStatus
            .OPEN.value
        ),
        server_default=(
            SafetyInspectionDefectStatus
            .OPEN.value
        ),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = (
        mapped_column(
            db.Text,
            nullable=False,
        )
    )
    immediate_action: Mapped[
        str | None
    ] = mapped_column(db.Text)
    review_note: Mapped[
        str | None
    ] = mapped_column(db.Text)
    reviewer_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
            index=True,
        )
    )
    resolved_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    resolved_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    dismissed_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    dismissed_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )

    job: Mapped[
        SafetyInspectionJob
    ] = relationship(
        back_populates="defects"
    )
    result: Mapped[
        SafetyInspectionResult
    ] = relationship(
        back_populates="defect"
    )
    reviewer: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[reviewer_user_id]
    )
    resolved_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[resolved_by_user_id]
    )
    dismissed_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[dismissed_by_user_id]
    )
    commission_links: Mapped[
        list[
            "SafetyInspectionDefectCommission"
        ]
    ] = relationship(
        back_populates="defect",
        cascade="all, delete-orphan",
    )


class SafetyInspectionDefectCommission(
    db.Model
):
    __tablename__ = (
        "safety_inspection_defect_commission"
    )
    __table_args__ = (
        UniqueConstraint(
            "defect_id",
            "commission_id",
            name=(
                "uq_safety_inspection_defect_"
                "commission"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    defect_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_defect.id"
        ),
        nullable=False,
        index=True,
    )
    commission_id: Mapped[int] = (
        mapped_column(
            db.ForeignKey("commission.id"),
            nullable=False,
            index=True,
        )
    )
    primary_link: Mapped[bool] = (
        mapped_column(
            db.Boolean,
            default=True,
            server_default=true(),
            nullable=False,
        )
    )
    created_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
        )
    )

    defect: Mapped[
        SafetyInspectionDefect
    ] = relationship(
        back_populates="commission_links"
    )
    commission: Mapped[
        Commission
    ] = relationship()
    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )


class SafetyInspectionProtocol(db.Model):
    __tablename__ = "safety_inspection_protocol"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "version_number",
            name=(
                "uq_safety_inspection_protocol_"
                "version"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        db.ForeignKey(
            "safety_inspection_job.id"
        ),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
        )
    )
    correction_reason: Mapped[
        str | None
    ] = mapped_column(db.Text)
    document_filename: Mapped[str] = (
        mapped_column(
            db.String(255),
            nullable=False,
        )
    )
    document_sha256: Mapped[str] = (
        mapped_column(
            db.String(64),
            nullable=False,
        )
    )
    dms_archive_status: Mapped[
        str
    ] = mapped_column(
        db.String(30),
        default=(
            SafetyInspectionArchiveStatus
            .NOT_ARCHIVED.value
        ),
        server_default=(
            SafetyInspectionArchiveStatus
            .NOT_ARCHIVED.value
        ),
        nullable=False,
        index=True,
    )
    dms_archive_key: Mapped[
        str | None
    ] = mapped_column(
        db.String(180),
        unique=True,
    )
    dms_document_id: Mapped[
        str | None
    ] = mapped_column(
        db.String(255),
        unique=True,
    )
    dms_archive_pdf_sha256: Mapped[
        str | None
    ] = mapped_column(db.String(64))
    dms_archive_attempted_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    dms_archived_at: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    dms_archived_by_user_id: Mapped[
        int | None
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        index=True,
    )
    dms_archive_error: Mapped[
        str | None
    ] = mapped_column(db.Text)
    created_by_user_id: Mapped[
        int
    ] = mapped_column(
        db.ForeignKey("user_account.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = (
        mapped_column(
            db.DateTime(timezone=True),
            default=utc_now,
            nullable=False,
            index=True,
        )
    )

    job: Mapped[
        SafetyInspectionJob
    ] = relationship(
        back_populates="protocols"
    )
    created_by: Mapped[User] = relationship(
        foreign_keys=[created_by_user_id]
    )
    dms_archived_by: Mapped[
        User | None
    ] = relationship(
        foreign_keys=[
            dms_archived_by_user_id
        ]
    )


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
    received_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False,
                                                  index=True)

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
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False,
                                                 index=True)

    actor: Mapped[User | None] = relationship()


class ApiTokenBlocklist(db.Model):
    __tablename__ = "api_token_blocklist"
    __table_args__ = (UniqueConstraint("jti", name="uq_api_token_blocklist_jti"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    jti: Mapped[str] = mapped_column(db.String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utc_now, nullable=False)


class WowiDepartment(db.Model):
    __tablename__ = "wowi_department"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    erp_id: Mapped[int] = mapped_column(
        db.Integer,
        unique=True,
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        index=True,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    craft_activity_mappings: Mapped[
        list["CraftActivityDepartmentMapping"]
    ] = relationship(
        back_populates="department",
    )


class CraftActivityDepartmentMapping(db.Model):
    __tablename__ = "craft_activity_department_mapping"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    craft_activity_id: Mapped[int] = mapped_column(
        db.ForeignKey("craft_activity.id"),
        unique=True,
        nullable=False,
        index=True,
    )
    department_id: Mapped[int] = mapped_column(
        db.ForeignKey("wowi_department.id"),
        nullable=False,
        index=True,
    )
    updated_by_user_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey("user_account.id"),
            index=True,
        )
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    craft_activity: Mapped[CraftActivity] = relationship(
        back_populates="department_mapping"
    )
    department: Mapped[WowiDepartment] = relationship(
        back_populates="craft_activity_mappings"
    )
    updated_by: Mapped[User | None] = relationship()


class WowiEconomicUnit(db.Model):
    __tablename__ = "wowi_economic_unit"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(
        db.Integer,
        unique=True,
        nullable=False,
        index=True,
    )
    id_num: Mapped[str | None] = mapped_column(
        db.String(100),
        index=True,
    )
    name: Mapped[str | None] = mapped_column(
        db.String(255),
        index=True,
    )
    location: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    company_code_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    company_code_code: Mapped[str | None] = mapped_column(
        db.String(40),
        index=True,
    )
    universal_responsibility: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        nullable=False,
    )
    universal_responsible_official_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    universal_responsible_person_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    buildings: Mapped[list["WowiBuilding"]] = relationship(
        back_populates="economic_unit"
    )
    use_units: Mapped[list["WowiUseUnit"]] = relationship(
        back_populates="economic_unit"
    )
    jurisdictions: Mapped[
        list["WowiEconomicUnitJurisdiction"]
    ] = relationship(
        back_populates="economic_unit",
        cascade="all, delete-orphan",
    )


class WowiEconomicUnitJurisdiction(db.Model):
    __tablename__ = "wowi_economic_unit_jurisdiction"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(
        db.Integer,
        unique=True,
        nullable=False,
        index=True,
    )
    economic_unit_id: Mapped[int] = mapped_column(
        db.ForeignKey("wowi_economic_unit.id"),
        nullable=False,
        index=True,
    )
    department_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    department_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    department_type_erp_id: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
        index=True,
    )
    department_type_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    responsible_official_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    responsible_person_erp_id: Mapped[
        int | None
    ] = mapped_column(
        db.Integer,
        index=True,
    )
    responsible_name: Mapped[str | None] = mapped_column(
        db.String(255)
    )
    main_jurisdiction: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    economic_unit: Mapped[WowiEconomicUnit] = relationship(
        back_populates="jurisdictions"
    )


class WowiLicenseAgreement(db.Model):
    __tablename__ = "wowi_license_agreement"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(
        db.Integer,
        unique=True,
        nullable=False,
        index=True,
    )
    id_num: Mapped[str | None] = mapped_column(
        db.String(100),
        index=True,
    )
    use_unit_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    use_unit_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("wowi_use_unit.id"),
        index=True,
    )
    start_date: Mapped[date | None] = mapped_column(db.Date)
    end_date: Mapped[date | None] = mapped_column(db.Date)
    is_vacancy: Mapped[bool] = mapped_column(
        db.Boolean,
        default=False,
        nullable=False,
        index=True,
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )

    use_unit: Mapped["WowiUseUnit | None"] = relationship(
        back_populates="license_agreements"
    )
    contractors: Mapped[list["WowiContractor"]] = relationship(
        back_populates="license_agreement",
        cascade="all, delete-orphan",
    )


class WowiContractor(db.Model):
    __tablename__ = "wowi_contractor"
    __table_args__ = (
        db.Index(
            (
                "ux_wowi_contractor_"
                "erp_license_agreement"
            ),
            "erp_id",
            "license_agreement_erp_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )
    erp_id: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False,
        index=True,
    )
    license_agreement_erp_id: Mapped[int] = (
        mapped_column(
            db.Integer,
            nullable=False,
            index=True,
        )
    )
    license_agreement_id: Mapped[int | None] = (
        mapped_column(
            db.ForeignKey(
                "wowi_license_agreement.id"
            ),
            index=True,
        )
    )
    person_erp_id: Mapped[int | None] = (
        mapped_column(
            db.Integer,
            index=True,
        )
    )
    display_name: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False,
        index=True,
    )
    contractual_use_from: Mapped[date | None] = (
        mapped_column(
            db.Date
        )
    )
    contractual_use_to: Mapped[date | None] = (
        mapped_column(
            db.Date
        )
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = (
        mapped_column(
            db.DateTime(timezone=True)
        )
    )

    license_agreement: Mapped[
        WowiLicenseAgreement | None
    ] = relationship(
        back_populates="contractors"
    )


class WowiBuilding(db.Model):
    __tablename__ = "wowi_building"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(db.Integer, unique=True, index=True, nullable=False)
    id_num: Mapped[str | None] = mapped_column(db.String(100), index=True)
    economic_unit_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    economic_unit_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("wowi_economic_unit.id"),
        index=True,
    )
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
    economic_unit: Mapped[
        WowiEconomicUnit | None
    ] = relationship(
        back_populates="buildings"
    )
    use_units: Mapped[list["WowiUseUnit"]] = relationship(
        back_populates="building"
    )


class WowiUseUnit(db.Model):
    __tablename__ = "wowi_use_unit"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(db.Integer, unique=True, index=True, nullable=False)
    id_num: Mapped[str | None] = mapped_column(db.String(100), index=True)
    economic_unit_erp_id: Mapped[int | None] = mapped_column(
        db.Integer,
        index=True,
    )
    economic_unit_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("wowi_economic_unit.id"),
        index=True,
    )
    building_erp_id: Mapped[int | None] = mapped_column(db.Integer, index=True)
    building_id: Mapped[int | None] = mapped_column(db.ForeignKey("wowi_building.id"), index=True)
    floor_name: Mapped[str | None] = mapped_column(db.String(255))
    position_name: Mapped[str | None] = mapped_column(db.String(255))
    description_of_position: Mapped[str | None] = mapped_column(db.String(255))
    address: Mapped[str | None] = mapped_column(db.String(500))
    exit_date: Mapped[
        datetime | None
    ] = mapped_column(
        db.DateTime(timezone=True)
    )
    active: Mapped[bool] = mapped_column(
        db.Boolean,
        default=True,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    economic_unit: Mapped[
        WowiEconomicUnit | None
    ] = relationship(
        back_populates="use_units"
    )
    building: Mapped[
        WowiBuilding | None
    ] = relationship(
        back_populates="use_units"
    )
    license_agreements: Mapped[
        list[WowiLicenseAgreement]
    ] = relationship(
        back_populates="use_unit"
    )
