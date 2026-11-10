from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from time import monotonic, sleep
from uuid import uuid4

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError

from .audit import audit
from .extensions import db
from .models import (
    CommissionTicketLink, HealthHeartbeat, TicketAiJob, TicketAiState,
    TicketMirror, TicketMirrorState, utc_now,
)
from .settings import SettingKeys, settings
from .ticket_service import (
    analyze_ticket_mirror, as_int, ticket_analysis_input_hash, value,
)


@dataclass(frozen=True)
class TicketAiClaim:
    ticket_id: int
    revision: int
    token: str
    actor_user_id: int | None


class TicketAiChanged(RuntimeError):
    pass


class TicketAiCancelled(RuntimeError):
    pass


def ticket_ai_enabled() -> bool:
    return bool(
        settings.get(SettingKeys.OPENAI_ENABLED)
        and str(settings.get(SettingKeys.OPENAI_API_KEY)).strip()
        and str(settings.get(SettingKeys.OPENAI_MODEL)).strip()
    )


def ticket_ai_is_current(ticket: TicketMirror) -> bool:
    if ticket.ai_state != TicketAiState.READY.value:
        return False
    try:
        return ticket.ai_input_hash == ticket_analysis_input_hash(ticket)
    except (ValueError, RuntimeError):
        return False


def automatic_ticket_ai_allowed(ticket: TicketMirror) -> bool:
    if (
        not settings.get(SettingKeys.TICKET_AI_PREFETCH_ENABLED)
        or ticket.deleted_at is not None
        or ticket.state != TicketMirrorState.NEW.value
        or ticket.commission_id is not None
    ):
        return False
    if CommissionTicketLink.query.filter_by(ticket_erp_id=ticket.erp_id).first():
        return False
    for assignment in ticket.assignments or []:
        if not isinstance(assignment, dict):
            continue
        entity = value(assignment, "AssignmentEntity")
        if (
            isinstance(entity, dict)
            and as_int(value(entity, "Id")) == 7
            and as_int(value(assignment, "EntityId"))
        ):
            return False
    return True


def enqueue_ticket_ai(
    ticket: TicketMirror,
    actor_user_id: int | None = None,
    *,
    automatic: bool = True,
    force: bool = False,
) -> bool:
    if not ticket_ai_enabled():
        return False
    db.session.flush()
    ticket = (
        TicketMirror.query.filter_by(id=ticket.id)
        .populate_existing().with_for_update().one()
    )
    if ticket.deleted_at is not None or ticket.state == TicketMirrorState.IGNORED.value:
        return False
    if automatic and not automatic_ticket_ai_allowed(ticket):
        return False
    job = db.session.get(TicketAiJob, ticket.id, with_for_update=True)
    if job is not None and not force and job.source_signature == ticket.source_signature:
        if not automatic:
            job.automatic = False
            job.actor_user_id = actor_user_id
        return True
    if job is None:
        job = TicketAiJob(ticket_id=ticket.id, revision=1, automatic=automatic)
        db.session.add(job)
    else:
        job.revision += 1
        job.automatic = job.automatic and automatic
    job.source_signature = ticket.source_signature
    job.actor_user_id = actor_user_id if actor_user_id is not None else job.actor_user_id
    job.available_at = utc_now()
    job.attempt_count = 0
    if force:
        for field_name in (
            "ai_candidate", "ai_classification", "ai_craft_activity_id",
            "ai_title", "ai_commission_text", "ai_damage_location",
            "ai_task_template_id", "ai_assignment_rule_id", "ai_assigned_user_id",
            "ai_assignment_mode", "ai_model", "ai_input_hash", "ai_generated_at",
        ):
            setattr(ticket, field_name, None)
        ticket.ai_missing_information = []
    ticket.ai_state = TicketAiState.QUEUED.value
    ticket.ai_error = None
    return True


def ticket_ai_lease_seconds() -> float:
    return max(
        600, float(settings.get(SettingKeys.OPENAI_TIMEOUT_SECONDS)) * 4 + 120
    )


def claim_ticket_ai() -> TicketAiClaim | None:
    lease_seconds = ticket_ai_lease_seconds()
    db.session.rollback()
    ticket = (
        TicketMirror.query.join(TicketAiJob, TicketAiJob.ticket_id == TicketMirror.id)
        .filter(
            or_(
                (TicketAiJob.locked_at.is_(None))
                & (TicketAiJob.available_at <= utc_now())
                & (TicketAiJob.attempt_count < 3),
                TicketAiJob.locked_at < utc_now() - timedelta(seconds=lease_seconds),
            )
        )
        .order_by(TicketAiJob.available_at, TicketAiJob.ticket_id)
        .with_for_update(skip_locked=True, of=TicketMirror)
        .first()
    )
    if ticket is None:
        db.session.rollback()
        return None
    job = db.session.get(TicketAiJob, ticket.id, with_for_update=True)
    if job.attempt_count >= 3:
        job.locked_at = None
        job.lock_token = None
        ticket.ai_state = TicketAiState.FAILED.value
        ticket.ai_error = "Die KI-Verarbeitung wurde wiederholt unterbrochen. Bitte neu erstellen."
        db.session.commit()
        return None
    if (
        not ticket_ai_enabled()
        or ticket.deleted_at is not None
        or ticket.state == TicketMirrorState.IGNORED.value
        or (job.automatic and not automatic_ticket_ai_allowed(ticket))
    ):
        db.session.delete(job)
        ticket.ai_state = (
            TicketAiState.STALE.value if ticket.ai_input_hash
            else TicketAiState.NOT_REQUESTED.value
        )
        ticket.ai_error = None
        db.session.commit()
        return None
    job.locked_at = utc_now()
    job.lock_token = uuid4().hex
    job.attempt_count += 1
    ticket.ai_state = TicketAiState.PROCESSING.value
    claim = TicketAiClaim(ticket.id, job.revision, job.lock_token, job.actor_user_id)
    db.session.commit()
    return claim


def validate_ticket_ai(
    claim: TicketAiClaim, ticket_id: int, input_hash: str,
) -> TicketMirror:
    retry_count = 0
    while True:
        db.session.rollback()
        try:
            ticket = (
                TicketMirror.query.filter_by(id=ticket_id)
                .populate_existing().with_for_update().one_or_none()
            )
            job = (
                TicketAiJob.query.filter_by(ticket_id=ticket_id)
                .populate_existing().with_for_update().one_or_none()
            )
            if (
                ticket is None or job is None
                or job.lock_token != claim.token or job.revision != claim.revision
            ):
                raise TicketAiChanged()
            if (
                not ticket_ai_enabled()
                or ticket.deleted_at is not None
                or ticket.state == TicketMirrorState.IGNORED.value
                or (job.automatic and not automatic_ticket_ai_allowed(ticket))
            ):
                raise TicketAiCancelled()
            if ticket_analysis_input_hash(ticket) != input_hash:
                raise TicketAiChanged()
            return ticket
        except OperationalError as error:
            db.session.rollback()
            error_code = error.orig.args[0] if error.orig.args else None
            if error_code != 1020 or retry_count >= 2:
                raise
            sleep(0.05 * 2 ** retry_count)
            retry_count += 1


def settle_ticket_ai(
    claim: TicketAiClaim, error: Exception, *, cancelled: bool = False,
) -> None:
    db.session.rollback()
    ticket = (
        TicketMirror.query.filter_by(id=claim.ticket_id)
        .populate_existing().with_for_update().one_or_none()
    )
    job = db.session.get(TicketAiJob, claim.ticket_id, with_for_update=True)
    if ticket is None or job is None or job.lock_token != claim.token:
        db.session.rollback()
        return
    job.locked_at = None
    job.lock_token = None
    if job.revision != claim.revision:
        ticket.ai_state = TicketAiState.QUEUED.value
        ticket.ai_error = None
        job.available_at = utc_now()
    elif cancelled:
        db.session.delete(job)
        ticket.ai_state = (
            TicketAiState.STALE.value if ticket.ai_input_hash
            else TicketAiState.NOT_REQUESTED.value
        )
        ticket.ai_error = None
    elif isinstance(error, TicketAiChanged):
        job.revision += 1
        job.source_signature = ticket.source_signature
        job.attempt_count = 0
        job.available_at = utc_now()
        ticket.ai_state = TicketAiState.QUEUED.value
        ticket.ai_error = None
    else:
        ticket.ai_error = str(error)[:2000]
        ticket.ai_state = (
            TicketAiState.FAILED.value if job.attempt_count >= 3
            else TicketAiState.QUEUED.value
        )
        job.available_at = utc_now() + timedelta(
            seconds=30 * 2 ** min(job.attempt_count - 1, 2)
        )
    db.session.commit()


def process_ticket_ai() -> bool:
    claim = claim_ticket_ai()
    if claim is None:
        return False
    try:
        ticket = db.session.get(TicketMirror, claim.ticket_id)
        ticket = analyze_ticket_mirror(
            ticket, claim.actor_user_id or 0,
            validate_result=lambda ticket_id, input_hash: validate_ticket_ai(
                claim, ticket_id, input_hash
            ),
        )
        job = db.session.get(TicketAiJob, claim.ticket_id)
        db.session.delete(job)
        audit(
            "ticket.ai.analyze", "ticket", ticket.erp_id,
            {
                "model": ticket.ai_model,
                "classification": ticket.ai_classification,
                "craft_activity_id": ticket.ai_craft_activity_id,
                "task_template_id": ticket.ai_task_template_id,
                "assignment_rule_id": ticket.ai_assignment_rule_id,
                "assigned_user_id": ticket.ai_assigned_user_id,
                "assignment_mode": ticket.ai_assignment_mode,
                "automatic": job.automatic,
            },
            actor_user_id=job.actor_user_id,
        )
        db.session.commit()
    except TicketAiCancelled as exc:
        settle_ticket_ai(claim, exc, cancelled=True)
    except TicketAiChanged as exc:
        settle_ticket_ai(claim, exc)
    except Exception as exc:
        current_app.logger.exception("ticket_ai_analysis_failed ticket_id=%s", claim.ticket_id)
        settle_ticket_ai(claim, exc)
    return True


def run_ticket_ai_worker(poll_seconds: int = 2, once: bool = False) -> None:
    last_heartbeat = None
    while True:
        try:
            if last_heartbeat is None or monotonic() - last_heartbeat >= 60:
                heartbeat = db.session.get(HealthHeartbeat, "ticket_ai_worker")
                if heartbeat is None:
                    heartbeat = HealthHeartbeat(name="ticket_ai_worker")
                    db.session.add(heartbeat)
                heartbeat.last_seen_at = utc_now()
                db.session.commit()
                last_heartbeat = monotonic()
            processed = process_ticket_ai()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("ticket_ai_worker_failed")
            processed = False
        finally:
            db.session.remove()
        if once:
            return
        if not processed:
            sleep(max(poll_seconds, 1))
