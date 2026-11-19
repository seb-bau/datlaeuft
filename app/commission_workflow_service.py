from __future__ import annotations

from .audit import audit
from .erp_sync import enqueue_erp_operation
from .models import (
    Commission,
    CommissionLocalStatus,
    CommissionSyncState,
    ErpOperationType,
    ExternalCraftsman,
    User,
    utc_now,
)
from .offer_service import sync_offer_from_commission
from .performance_review_service import (
    commission_requires_material_confirmation,
)


class CommissionWorkflowError(ValueError):
    def __init__(
        self,
        message: str,
        status_code: int,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def cancel_commission(commission: Commission, *, user: User) -> None:
    if not commission.can_be_canceled:
        raise CommissionWorkflowError("Der Auftrag kann in diesem Status nicht storniert werden.", 409)

    commission.local_status = CommissionLocalStatus.CANCELED.value
    commission.status_changed_at = utc_now()
    enqueue_erp_operation(
        commission,
        ErpOperationType.CANCEL_COMMISSION_FROM_OFFICE,
        f"commission:office-canceled:{commission.id}",
        actor_user_id=user.id,
    )
    audit("commission.cancel", "commission", commission.id, actor_user_id=user.id)
    sync_offer_from_commission(commission)


def accept_commission(
    commission: Commission,
    *,
    user: User | None = None,
    external_craftsman: ExternalCraftsman | None = None,
) -> None:
    validate_actor(user, external_craftsman)
    validate_actionable(commission)

    if (
        commission.local_status
        != CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value
    ):
        raise CommissionWorkflowError(
            "Der Auftrag kann in diesem Status nicht gestartet werden.",
            409,
        )

    commission.local_status = (
        CommissionLocalStatus.IN_PROGRESS.value
    )
    commission.status_changed_at = utc_now()
    commission.erp_sync_state = (
        CommissionSyncState.PENDING.value
    )
    commission.erp_sync_message = None

    enqueue_erp_operation(
        commission,
        ErpOperationType.ACCEPT_COMMISSION,
        f"commission:accepted:{commission.id}",
        actor_user_id=user.id if user else None,
    )

    actor_user_id, actor_external_id = actor_ids(
        user,
        external_craftsman,
    )
    audit(
        "commission.start",
        "commission",
        commission.id,
        actor_user_id=actor_user_id,
        actor_external_craftsman_id=actor_external_id,
    )


def reject_commission(
    commission: Commission,
    reason: str,
    *,
    user: User | None = None,
    external_craftsman: ExternalCraftsman | None = None,
) -> str:
    validate_actor(user, external_craftsman)
    validate_actionable(commission)

    if (
        commission.local_status
        != CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value
    ):
        raise CommissionWorkflowError(
            "Nur ein noch nicht angenommener Auftrag kann abgelehnt werden.",
            409,
        )

    normalized_reason = reason.strip()

    if not normalized_reason:
        raise CommissionWorkflowError(
            "Bitte einen Grund für die Ablehnung angeben.",
            400,
        )

    now = utc_now()
    commission.rejected_at = now
    commission.completion_successful = False
    commission.tenant_charge_requested = False
    commission.completion_reason = normalized_reason
    set_completion_actor(
        commission,
        user=user,
        external_craftsman=external_craftsman,
    )
    commission.completion_submitted_at = now
    commission.completed_at = None
    commission.local_status = (
        CommissionLocalStatus.COMPLETION_REVIEW.value
    )
    commission.status_changed_at = now
    commission.erp_sync_state = (
        CommissionSyncState.PENDING.value
    )
    commission.erp_sync_message = None

    enqueue_erp_operation(
        commission,
        ErpOperationType.REFUSE_COMMISSION,
        f"commission:refused:{commission.id}",
        actor_user_id=user.id if user else None,
    )

    actor_user_id, actor_external_id = actor_ids(
        user,
        external_craftsman,
    )
    audit(
        "commission.reject",
        "commission",
        commission.id,
        {"reason": normalized_reason},
        actor_user_id=actor_user_id,
        actor_external_craftsman_id=actor_external_id,
    )
    sync_offer_from_commission(commission)
    return normalized_reason


def complete_commission(
    commission: Commission,
    *,
    completed: bool,
    reason: str,
    tenant_charge: bool = False,
    confirm_missing_material: bool = False,
    enforce_material_confirmation: bool = True,
    user: User | None = None,
    external_craftsman: ExternalCraftsman | None = None,
) -> str:
    validate_actor(user, external_craftsman)
    validate_actionable(commission)

    if (
        commission.local_status
        != CommissionLocalStatus.IN_PROGRESS.value
    ):
        raise CommissionWorkflowError(
            "Nur ein Auftrag in Bearbeitung kann abgeschlossen werden.",
            409,
        )

    if (
        completed
        and enforce_material_confirmation
        and commission_requires_material_confirmation(
            commission
        )
        and not confirm_missing_material
    ):
        raise CommissionWorkflowError(
            (
                "Für diesen Auftrag ist eine Materialbuchung "
                "vorgesehen. Auftrag trotzdem abschließen?"
            ),
            409,
            "material_booking_confirmation_required",
        )

    normalized_reason = reason.strip()

    if not completed:
        tenant_charge = False

        if not normalized_reason:
            raise CommissionWorkflowError(
                (
                    "Bitte einen Grund angeben, warum der "
                    "Auftrag nicht abgeschlossen werden konnte."
                ),
                400,
            )

    now = utc_now()
    commission.completion_successful = completed
    commission.tenant_charge_requested = tenant_charge
    commission.completion_reason = normalized_reason or None
    set_completion_actor(
        commission,
        user=user,
        external_craftsman=external_craftsman,
    )
    commission.completion_submitted_at = now
    commission.status_changed_at = now
    operation_type = None

    if not completed:
        commission.local_status = (
            CommissionLocalStatus.COMPLETION_REVIEW.value
        )
        commission.completed_at = None
    elif tenant_charge:
        commission.local_status = (
            CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
        )
        commission.completed_at = now
        operation_type = ErpOperationType.AWAIT_INVOICE
        operation_key = (
            f"commission:await-invoice:{commission.id}"
        )
    else:
        commission.local_status = (
            CommissionLocalStatus.DONE.value
        )
        commission.completed_at = now
        operation_type = ErpOperationType.COMPLETE_COMMISSION
        operation_key = f"commission:done:{commission.id}"

    if operation_type is not None:
        commission.erp_sync_state = CommissionSyncState.PENDING.value
        commission.erp_sync_message = None
        enqueue_erp_operation(
            commission,
            operation_type,
            operation_key,
            actor_user_id=user.id if user else None,
        )

    actor_user_id, actor_external_id = actor_ids(
        user,
        external_craftsman,
    )
    audit(
        "commission.complete",
        "commission",
        commission.id,
        {
            "completed": completed,
            "tenant_charge": tenant_charge,
            "reason": normalized_reason or None,
        },
        actor_user_id=actor_user_id,
        actor_external_craftsman_id=actor_external_id,
    )
    sync_offer_from_commission(commission)

    if completed:
        from .work_report_document import (
            WorkReportDocumentError,
            commission_work_report_entries,
            create_work_report_pdf,
        )

        try:
            create_work_report_pdf(
                commission,
                commission_work_report_entries(commission),
            )
        except WorkReportDocumentError as exc:
            raise CommissionWorkflowError(
                str(exc),
                500,
                "work_report_creation_failed",
            ) from exc

    return normalized_reason


def validate_actionable(
    commission: Commission,
) -> None:
    if commission.local_status == CommissionLocalStatus.CANCELED.value:
        raise CommissionWorkflowError("Dieser Auftrag wurde storniert.", 409)
    if commission.rejected_at is not None or commission.review_resolved_at is not None:
        raise CommissionWorkflowError("Dieser Auftrag wurde bereits abgelehnt oder abschließend geprüft.", 409)
    if not commission.is_actionable:
        raise CommissionWorkflowError(
            (
                "Der Auftrag wartet noch auf eine "
                "OPENWOWI-Auftragsnummer."
            ),
            409,
        )


def validate_actor(
    user: User | None,
    external_craftsman: ExternalCraftsman | None,
) -> None:
    if (user is None) == (external_craftsman is None):
        raise ValueError(
            "Es muss genau ein handelnder Benutzer angegeben werden."
        )


def actor_ids(
    user: User | None,
    external_craftsman: ExternalCraftsman | None,
) -> tuple[int | None, int | None]:
    return (
        user.id if user else None,
        external_craftsman.id
        if external_craftsman
        else None,
    )


def set_completion_actor(
    commission: Commission,
    *,
    user: User | None,
    external_craftsman: ExternalCraftsman | None,
) -> None:
    commission.completion_submitted_by = user
    commission.completion_submitted_by_external_craftsman = (
        external_craftsman
    )
