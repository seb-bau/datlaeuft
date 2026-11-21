from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
import secrets
from typing import Any

from flask import current_app
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .extensions import db
from .external_branding import (
    get_external_portal_name,
    send_external_email,
)
from .models import (
    Commission,
    CommissionLocalStatus,
    CraftActivity,
    ExternalCommissionAssignmentMail,
    ExternalCraftsman,
    ExternalCraftsmanAccessToken,
    User,
    utc_now,
)
from .settings import SettingKeys, settings
from .user_sync import (
    extract_craft_activity_ids,
    extract_craftsman_display_name,
    extract_craftsman_email,
    extract_craftsman_erp_id,
)
from .blacklist import include_blacklisted
from .email_templates import apply_email_template, commission_email_context, set_email_content
from .external_order_responses import (
    assignment_is_current, automatic_external_mails_enabled, ensure_external_followups,
    portal_light_enabled, response_url, set_external_order_content,
)


@dataclass
class ExternalCraftsmanSyncResult:
    seen: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    skipped: int = 0


def available_external_craftsmen(
    craft_activity_id: int | None = None,
) -> list[ExternalCraftsman]:
    internal_craftsman_ids = db.session.query(
        User.craftsman_erp_id
    ).filter(
        User.is_active_flag.is_(True),
        User.craftsman_erp_id.is_not(None),
    )

    query = (
        ExternalCraftsman.query
        .options(
            selectinload(
                ExternalCraftsman.craft_activities
            )
        )
        .filter(
            ExternalCraftsman.assignment_enabled.is_(True),
            ExternalCraftsman.wowi_active.is_(True),
            ExternalCraftsman.craftsman_erp_id.not_in(
                internal_craftsman_ids
            ),
        )
    )

    if craft_activity_id is not None:
        query = query.filter(
            or_(
                ~ExternalCraftsman.craft_activities.any(),
                ExternalCraftsman.craft_activities.any(
                    CraftActivity.id == craft_activity_id
                ),
            )
        )

    return (
        query
        .order_by(
            ExternalCraftsman.display_name.asc(),
            ExternalCraftsman.id.asc(),
        )
        .all()
    )


def automatic_external_craftsman(
    craft_activity: CraftActivity,
    candidates: list[
        ExternalCraftsman
    ] | None = None,
) -> ExternalCraftsman | None:
    available_candidates = (
        candidates
        if candidates is not None
        else available_external_craftsmen(
            craft_activity.id
        )
    )

    preferred_id = (
        craft_activity
        .preferred_external_craftsman_id
    )

    if preferred_id is not None:
        preferred_candidate = next(
            (
                candidate
                for candidate
                in available_candidates
                if candidate.id == preferred_id
            ),
            None,
        )

        if preferred_candidate is not None:
            return preferred_candidate

    if available_candidates:
        return secrets.choice(available_candidates)

    return None


def resolve_external_craftsman(
    craft_activity: CraftActivity,
    external_craftsman_id: int | None,
) -> ExternalCraftsman:
    candidates = available_external_craftsmen(
        craft_activity.id
    )

    if external_craftsman_id is not None:
        selected_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate.id
                == external_craftsman_id
            ),
            None,
        )

        if selected_candidate is None:
            raise ValueError(
                "Der ausgewählte externe "
                "Handwerker ist für dieses "
                "Gewerk nicht verfügbar."
            )

        return selected_candidate

    if not candidates:
        raise ValueError(
            "Für dieses Gewerk ist kein externer "
            "Handwerker verfügbar."
        )

    automatic_candidate = automatic_external_craftsman(
        craft_activity,
        candidates,
    )

    if automatic_candidate is None:
        raise ValueError(
            "Für dieses Gewerk konnte kein "
            "externer Handwerker ermittelt werden."
        )

    return automatic_candidate


@include_blacklisted()
def sync_external_craftsmen_from_wowiport(
    rows: list[Any],
) -> ExternalCraftsmanSyncResult:
    result = ExternalCraftsmanSyncResult()
    now = utc_now()
    seen_erp_ids: set[int] = set()
    craft_activities_by_erp_id = {
        activity.erp_id: activity
        for activity in CraftActivity.query.filter(
            CraftActivity.erp_id.is_not(None)
        ).all()
    }

    for row in rows:
        result.seen += 1
        craftsman_erp_id = extract_craftsman_erp_id(
            row
        )

        if craftsman_erp_id is None:
            result.skipped += 1
            continue

        seen_erp_ids.add(craftsman_erp_id)
        item = ExternalCraftsman.query.filter_by(
            craftsman_erp_id=craftsman_erp_id
        ).one_or_none()
        created = item is None

        if item is None:
            item = ExternalCraftsman(
                craftsman_erp_id=craftsman_erp_id,
                display_name=(
                    f"Handwerker {craftsman_erp_id}"
                ),
                wowi_active=True,
            )
            db.session.add(item)

        previous_values = (
            item.display_name,
            item.email,
            item.wowi_active,
            frozenset(
                activity.id
                for activity in item.craft_activities
            ),
        )
        email = extract_craftsman_email(row)
        activity_ids = extract_craft_activity_ids(
            row
        )
        item.display_name = (
            extract_craftsman_display_name(
                row,
                email
                or f"Handwerker {craftsman_erp_id}",
            )
        )
        item.email = email
        item.wowi_active = True
        item.last_synced_at = now
        if activity_ids:
            missing_activity_ids = activity_ids.difference(
                craft_activities_by_erp_id
            )
            if missing_activity_ids:
                raise ValueError(
                    f"Handwerker {craftsman_erp_id}: "
                    "Unbekannte Wowiport-Gewerke-IDs: "
                    + ", ".join(map(str, sorted(missing_activity_ids)))
                )
            item.craft_activities = [
                craft_activities_by_erp_id[erp_id]
                for erp_id in sorted(activity_ids)
            ]
        current_values = (
            item.display_name,
            item.email,
            item.wowi_active,
            frozenset(
                activity.id
                for activity in item.craft_activities
            ),
        )

        if created:
            result.created += 1
        elif previous_values != current_values:
            result.updated += 1
        else:
            result.unchanged += 1

    if seen_erp_ids:
        missing_query = ExternalCraftsman.query.filter(
            ExternalCraftsman.wowi_active.is_(True),
            ExternalCraftsman.craftsman_erp_id.notin_(
                seen_erp_ids
            ),
        )

        for item in missing_query.all():
            item.wowi_active = False
            item.last_synced_at = now
            result.deactivated += 1

    return result


def default_external_username(
    craftsman: ExternalCraftsman,
) -> str:
    return f"handwerker-{craftsman.craftsman_erp_id}"


def create_invitation_token(
    craftsman: ExternalCraftsman,
    created_by_user_id: int,
) -> str:
    now = utc_now()
    ExternalCraftsmanAccessToken.query.filter(
        ExternalCraftsmanAccessToken.external_craftsman_id
        == craftsman.id,
        ExternalCraftsmanAccessToken.purpose
        == "invitation",
        ExternalCraftsmanAccessToken.used_at.is_(None),
    ).update(
        {
            ExternalCraftsmanAccessToken.used_at: now,
        },
        synchronize_session=False,
    )

    raw_token = secrets.token_urlsafe(32)
    db.session.add(
        ExternalCraftsmanAccessToken(
            external_craftsman=craftsman,
            created_by_user_id=created_by_user_id,
            purpose="invitation",
            token_hash=hash_external_token(raw_token),
            expires_at=now + timedelta(hours=48),
        )
    )
    return raw_token


def hash_external_token(raw_token: str) -> str:
    return sha256(
        raw_token.encode("utf-8")
    ).hexdigest()


def external_token_is_current(
    token: ExternalCraftsmanAccessToken,
) -> bool:
    return (
        token.used_at is None
        and as_utc(token.expires_at) > utc_now()
        and token.purpose == "invitation"
    )


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def validate_external_password(
    password: str,
) -> str | None:
    if len(password) < 12:
        return (
            "Das Kennwort muss mindestens 12 Zeichen "
            "lang sein."
        )

    if len(password) > 128:
        return (
            "Das Kennwort darf höchstens 128 Zeichen "
            "lang sein."
        )

    return None


def external_mail_configuration(
) -> tuple[str, str, int, str]:
    public_base_url = settings.get(
        SettingKeys.PUBLIC_BASE_URL
    ).strip().rstrip("/")
    smtp_host = settings.get(
        SettingKeys.SMTP_HOST
    ).strip()
    smtp_port = settings.get(
        SettingKeys.SMTP_PORT
    )
    smtp_sender = settings.get(
        SettingKeys.SMTP_SENDER
    ).strip()

    if not public_base_url:
        raise RuntimeError(
            "Es ist keine öffentliche Basis-URL konfiguriert."
        )

    if not smtp_host or not smtp_sender:
        raise RuntimeError(
            "SMTP-Server oder Absenderadresse fehlen."
        )

    return (
        public_base_url,
        smtp_host,
        smtp_port,
        smtp_sender,
    )


def send_external_invitation_email(
    craftsman: ExternalCraftsman,
    raw_token: str,
) -> None:
    if not craftsman.email:
        raise RuntimeError(
            "Für den Handwerker ist keine E-Mail-Adresse hinterlegt."
        )

    (
        public_base_url,
        smtp_host,
        smtp_port,
        smtp_sender,
    ) = external_mail_configuration()
    invitation_url = (
        f"{public_base_url}/auftragnehmer/einladung/"
        f"{raw_token}"
    )

    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = craftsman.email
    apply_email_template(message, "external_invitation", {
        "portal_name": get_external_portal_name(),
        "recipient_name": craftsman.display_name or "",
        "username": craftsman.username or "",
        "invitation_url": invitation_url,
        "valid_hours": 48,
    })
    send_external_email(
        message,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )


def ensure_external_assignment_mails(
    limit: int = 200,
) -> int:
    rows = (
        db.session.query(
            Commission,
            ExternalCraftsman,
        )
        .join(
            ExternalCraftsman,
            ExternalCraftsman.craftsman_erp_id
            == Commission.craftsman_erp_id,
        )
        .outerjoin(
            ExternalCommissionAssignmentMail,
            and_(
                ExternalCommissionAssignmentMail.commission_id
                == Commission.id,
                ExternalCommissionAssignmentMail.assignment_version
                == Commission.external_assignment_version,
            ),
        )
        .filter(
            or_(
                ExternalCraftsman.portal_enabled.is_(True), ExternalCraftsman.portal_light_enabled.is_(True),
                and_(ExternalCraftsman.portal_light_enabled.is_(None), settings.get(SettingKeys.EXTERNAL_PORTAL_LIGHT_DEFAULT)),
                ExternalCraftsman.reminder_first_days > 0, ExternalCraftsman.reminder_second_days > 0,
                ExternalCraftsman.cancel_unanswered_days > 0,
            ),
            ExternalCraftsman.wowi_active.is_(True),
            ExternalCraftsman.email.is_not(None),
            Commission.deleted_at.is_(None),
            Commission.user_id.is_(None),
            ExternalCraftsman.craftsman_erp_id.not_in(
                db.session.query(User.craftsman_erp_id).filter(User.is_active_flag.is_(True), User.craftsman_erp_id.is_not(None))
            ),
            Commission.is_retrospective.is_(False),
            Commission.approval_state != "pending",
            Commission.local_status.in_(
                (
                    CommissionLocalStatus.OPEN.value,
                    CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value,
                    CommissionLocalStatus.IN_PROGRESS.value,
                )
            ),
            ExternalCommissionAssignmentMail.id.is_(None),
        )
        .order_by(Commission.id.asc())
        .limit(limit)
        .all()
    )

    for commission, craftsman in rows:
        db.session.add(
            ExternalCommissionAssignmentMail(
                commission=commission,
                external_craftsman=craftsman,
                assignment_version=(
                    commission.external_assignment_version
                ),
                state="pending",
                available_at=utc_now(),
            )
        )

    return len(rows)


def process_external_assignment_mail_batch(
    limit: int = 20,
) -> int:
    now = utc_now()
    ExternalCommissionAssignmentMail.query.filter(
        ExternalCommissionAssignmentMail.state
        == "sending",
        ExternalCommissionAssignmentMail.locked_at
        < now - timedelta(minutes=15),
    ).update(
        {
            ExternalCommissionAssignmentMail.state:
                "failed",
            ExternalCommissionAssignmentMail.available_at:
                now,
            ExternalCommissionAssignmentMail.locked_at:
                None,
        },
        synchronize_session=False,
    )

    try:
        ensure_external_assignment_mails()
        ensure_external_followups()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()

    processed = 0

    while processed < limit:
        mail_id = claim_external_assignment_mail()

        if mail_id is None:
            break

        try:
            execute_external_assignment_mail(mail_id)
        except Exception as exc:
            fail_external_assignment_mail(
                mail_id,
                exc,
            )

        processed += 1

    return processed


def claim_external_assignment_mail() -> int | None:
    item = (
        ExternalCommissionAssignmentMail.query
        .filter(
            ExternalCommissionAssignmentMail.state.in_(
                ("pending", "failed")
            ),
            ExternalCommissionAssignmentMail.available_at
            <= utc_now(),
        )
        .order_by(
            ExternalCommissionAssignmentMail.available_at.asc(),
            ExternalCommissionAssignmentMail.id.asc(),
        )
        .with_for_update(skip_locked=True)
        .first()
    )

    if item is None:
        db.session.rollback()
        return None

    item.state = "sending"
    item.locked_at = utc_now()
    item.attempt_count += 1
    item_id = item.id
    db.session.commit()
    return item_id


def execute_external_assignment_mail(
    mail_id: int,
) -> None:
    item = db.session.get(
        ExternalCommissionAssignmentMail,
        mail_id,
    )

    if item is None:
        return

    commission = item.commission
    craftsman = item.external_craftsman
    current_assignment = assignment_is_current(item)
    access_is_current = (
        automatic_external_mails_enabled(craftsman)
        and craftsman.wowi_active
        and bool(craftsman.email)
    )

    if (commission.approval_state == "pending" or commission.is_retrospective or not current_assignment or not access_is_current
            or commission.deleted_at is not None or commission.user_id is not None or commission.local_status not in (
                CommissionLocalStatus.OPEN.value, CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value, CommissionLocalStatus.IN_PROGRESS.value,
            )):
        item.state = "discarded"
        item.locked_at = None
        item.last_error = None
        db.session.commit()
        return

    if (not commission.erp_id or not commission.erp_idnum or commission.erp_sync_state != "synced"
            or commission.local_status == CommissionLocalStatus.OPEN.value):
        item.state = "pending"
        item.available_at = utc_now() + timedelta(minutes=1)
        item.locked_at = None
        db.session.commit()
        return
    send_external_assignment_email(item)
    item.state = "sent"
    item.sent_at = utc_now()
    item.locked_at = None
    item.last_error = None
    db.session.commit()


def fail_external_assignment_mail(
    mail_id: int,
    error: Exception,
) -> None:
    db.session.rollback()
    item = db.session.get(
        ExternalCommissionAssignmentMail,
        mail_id,
    )

    if item is None:
        return

    delay_seconds = min(
        60 * (2 ** min(max(item.attempt_count - 1, 0), 6)),
        3600,
    )
    item.state = "failed"
    item.available_at = (
        utc_now()
        + timedelta(seconds=delay_seconds)
    )
    item.locked_at = None
    item.last_error = str(error)[:4000]
    db.session.commit()

    current_app.logger.exception(
        "external_assignment_email_failed mail_id=%s",
        mail_id,
    )


def send_external_assignment_email(
    item: ExternalCommissionAssignmentMail,
) -> None:
    commission = item.commission
    craftsman = item.external_craftsman
    (
        public_base_url,
        smtp_host,
        smtp_port,
        smtp_sender,
    ) = external_mail_configuration()
    commission_url = (
        f"{public_base_url}/auftragnehmer/auftraege/"
        f"{commission.public_id}"
    )
    if portal_light_enabled(craftsman) and commission.local_status == CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value:
        commission_url = response_url(item)
    elif not craftsman.portal_enabled:
        commission_url = ""
    context = commission_email_context(commission)
    context.update({
        "recipient_name": craftsman.display_name or "",
        "commission_url": commission_url,
    })
    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = craftsman.email
    content = apply_email_template(message, "external_assignment", context)
    set_external_order_content(message, content["subject"], commission.with_contact_information(content["body"]) + "\n", item)
    send_external_email(
        message,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )
