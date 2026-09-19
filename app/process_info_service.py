from __future__ import annotations

from datetime import date, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Any
from uuid import UUID

import requests
from flask import current_app
from sqlalchemy import and_, distinct, func, or_

from .extensions import db
from .mail_service import send_email
from .models import (
    Commission,
    CommissionLocalStatus,
    ProcessInfo,
    ProcessInfoRead,
    ProcessInfoThread,
    User,
    utc_now,
)
from .settings import SettingKeys, settings
from .wowiport import WowiportService


OFFICE_DIRECTION = "office"
CRAFTSMAN_DIRECTION = "craftsman"


def normalize_request_id(value: str) -> str:
    try:
        return str(UUID(str(value).strip()))
    except (
        TypeError,
        ValueError,
        AttributeError,
    ) as exc:
        raise ValueError(
            "Die Anfrage-ID ist ungültig."
        ) from exc


def normalize_info_body(value: Any) -> str:
    body = str(value or "").strip()

    if not body:
        raise ValueError(
            "Bitte eine Information eingeben."
        )

    if len(body) > 2000:
        raise ValueError(
            "Die Information darf höchstens "
            "2.000 Zeichen lang sein."
        )

    return body


def commissions_for_process(
    process_erp_id: int,
    user_id: int | None = None,
) -> list[Commission]:
    query = Commission.query.filter(
        Commission.crafts_process_erp_id
        == process_erp_id,
        Commission.deleted_at.is_(None),
        Commission.local_status
        != CommissionLocalStatus.DELETED.value,
    )

    if user_id is not None:
        query = query.filter(
            Commission.user_id == user_id
        )

    return query.order_by(
        Commission.id.asc()
    ).all()


def process_users(
    process_erp_id: int,
) -> list[tuple[User, Commission]]:
    rows = (
        db.session.query(User, Commission)
        .join(
            Commission,
            Commission.user_id == User.id,
        )
        .filter(
            Commission.crafts_process_erp_id
            == process_erp_id,
            Commission.deleted_at.is_(None),
            Commission.local_status
            != CommissionLocalStatus.DELETED.value,
            User.is_active_flag.is_(True),
        )
        .order_by(
            User.id.asc(),
            Commission.id.asc(),
        )
        .all()
    )

    result: list[tuple[User, Commission]] = []
    seen: set[int] = set()

    for user, commission in rows:
        if user.id not in seen:
            result.append((user, commission))
            seen.add(user.id)

    return result


def ensure_process_thread(
    process_erp_id: int,
) -> ProcessInfoThread:
    thread = db.session.get(
        ProcessInfoThread,
        process_erp_id,
    )

    if thread is None:
        thread = ProcessInfoThread(
            process_erp_id=process_erp_id
        )
        db.session.add(thread)
        db.session.flush()

    return thread


def sync_process_infos(
    process_erp_id: int,
    *,
    notify: bool,
    service: WowiportService | None = None,
) -> int:
    client = service or WowiportService()
    rows = client.get_process_notes(
        process_erp_id
    )
    thread = ensure_process_thread(
        process_erp_id
    )
    initial_sync = (
            thread.last_synced_at is None
    )
    should_notify = (
            notify
            and not initial_sync
    )

    existing_items = {
        item.erp_note_id: item
        for item in ProcessInfo.query.filter(
            ProcessInfo.process_erp_id
            == process_erp_id,
            ProcessInfo.erp_note_id.is_not(None),
        ).all()
    }

    remote_ids: set[int] = set()
    changed_items: list[ProcessInfo] = []

    for row in rows:
        note_id = _integer_value(
            _row_value(row, "Id")
        )
        body = str(
            _row_value(row, "Description")
            or ""
        ).strip()

        if note_id is None or not body:
            continue

        remote_ids.add(note_id)
        item = existing_items.get(note_id)

        if item is None:
            outgoing_candidates = [
                candidate
                for candidate
                in ProcessInfo.query.filter(
                    ProcessInfo.process_erp_id
                    == process_erp_id,
                    ProcessInfo.erp_note_id.is_(None),
                    ProcessInfo.direction
                    == CRAFTSMAN_DIRECTION,
                    ProcessInfo.erp_body == body,
                    ProcessInfo.state.in_(
                        (
                            "pending",
                            "failed",
                            "uncertain",
                        )
                    ),
                ).all()
                if note_id not in set(
                    candidate.erp_note_ids_before
                    or []
                )
            ]

            if len(outgoing_candidates) == 1:
                item = outgoing_candidates[0]
                item.erp_note_id = note_id
                item.erp_date = _date_value(
                    _row_value(row, "Date")
                )
                item.state = "synced"
                item.mail_state = "pending"
                item.mail_available_at = utc_now()
                item.last_error = None
                existing_items[note_id] = item
                continue

            item = ProcessInfo(
                process_erp_id=process_erp_id,
                erp_note_id=note_id,
                direction=OFFICE_DIRECTION,
                body=body,
                erp_body=body,
                author_name=_note_author(row),
                erp_date=_date_value(
                    _row_value(row, "Date")
                ),
                state="synced",
            )
            db.session.add(item)
            changed_items.append(item)
            continue

        if item.erp_body != body:
            item.erp_body = body

            if (
                item.direction
                == OFFICE_DIRECTION
            ):
                item.body = body

            item.version += 1
            changed_items.append(item)

        item.author_name = (
            item.author_name
            or _note_author(row)
        )
        item.erp_date = _date_value(
            _row_value(row, "Date")
        )
        item.state = "synced"
        item.last_error = None

    for item in existing_items.values():
        if (
            item.erp_note_id not in remote_ids
            and item.state == "synced"
        ):
            item.state = "deleted"

    now = utc_now()

    ProcessInfo.query.filter(
        ProcessInfo.process_erp_id
        == process_erp_id,
        ProcessInfo.state == "uncertain",
        ProcessInfo.updated_at
        < now - timedelta(minutes=10),
    ).update(
        {
            ProcessInfo.state: "failed",
            ProcessInfo.last_error: (
                "Die Übertragung wurde in "
                "Wowiport nicht gefunden."
            ),
        },
        synchronize_session=False,
    )

    thread.last_synced_at = now
    thread.next_sync_at = now + timedelta(
        minutes=max(
            settings.get(
                SettingKeys
                .PROCESS_INFO_SYNC_INTERVAL_MINUTES
            ),
            1,
        )
    )
    thread.last_error = None
    thread.updated_at = now

    db.session.flush()

    recipients = process_users(
        process_erp_id
    )

    if should_notify:
        from .push_service import (
            enqueue_process_info_push,
        )

        for item in changed_items:
            if (
                item.direction
                != OFFICE_DIRECTION
            ):
                continue

            for user, commission in recipients:
                enqueue_process_info_push(
                    user=user,
                    commission=commission,
                    info=item,
                    badge=unread_count_for_user(
                        user.id
                    ),
                )
    else:
        for item in changed_items:
            if (
                item.direction
                != OFFICE_DIRECTION
            ):
                continue

            for user, _ in recipients:
                mark_info_read(
                    item,
                    user.id,
                )

    return len(changed_items)


def create_outgoing_info(
    commission: Commission,
    user: User,
    request_id: str,
    body: str,
) -> ProcessInfo:
    process_erp_id = (
        commission.crafts_process_erp_id
    )

    if process_erp_id is None:
        raise ValueError(
            "Der Auftrag ist keinem "
            "Wowiport-Vorgang zugeordnet."
        )

    normalized_id = normalize_request_id(
        request_id
    )
    normalized_body = normalize_info_body(
        body
    )

    existing = db.session.get(
        ProcessInfo,
        normalized_id,
    )

    if existing is not None:
        if (
            existing.process_erp_id
            != process_erp_id
            or existing.sender_user_id
            != user.id
            or existing.body
            != normalized_body
        ):
            raise ValueError(
                "Die Anfrage-ID wurde "
                "bereits verwendet."
            )

        return existing

    erp_body = (
        f"{user.display_name} (WattNu):\n"
        f"{normalized_body}"
    )

    item = ProcessInfo(
        id=normalized_id,
        process_erp_id=process_erp_id,
        sender_user_id=user.id,
        direction=CRAFTSMAN_DIRECTION,
        body=normalized_body,
        erp_body=erp_body,
        author_name=user.display_name,
        state="pending",
        mail_state="not_required",
    )

    db.session.add(item)
    ensure_process_thread(process_erp_id)
    db.session.commit()

    return item


def write_outgoing_info(
    info_id: str,
    service: WowiportService | None = None,
) -> ProcessInfo:
    item = db.session.get(
        ProcessInfo,
        info_id,
    )

    if item is None:
        raise ValueError(
            "Die Information wurde "
            "nicht gefunden."
        )

    if item.state == "synced":
        return item

    client = service or WowiportService()

    try:
        rows = client.get_process_notes(
            item.process_erp_id
        )

        if item.erp_note_ids_before is None:
            item.erp_note_ids_before = [
                note_id
                for note_id in (
                    _integer_value(
                        _row_value(row, "Id")
                    )
                    for row in rows
                )
                if note_id is not None
            ]
            db.session.commit()

        candidates = [
            row
            for row in rows
            if (
                str(
                    _row_value(
                        row,
                        "Description",
                    )
                    or ""
                ).strip()
                == item.erp_body
                and _integer_value(
                    _row_value(row, "Id")
                )
                not in set(
                    item.erp_note_ids_before
                    or []
                )
            )
        ]

        if len(candidates) == 1:
            note_id = _integer_value(
                _row_value(
                    candidates[0],
                    "Id",
                )
            )

            if note_id is None:
                raise RuntimeError(
                    "Wowiport hat keine "
                    "Notiz-ID zurückgegeben."
                )

        elif item.state == "uncertain":
            raise RuntimeError(
                "Der Übertragungsstatus ist "
                "noch unklar. Die Information "
                "wird beim nächsten Abgleich "
                "geprüft."
            )

        else:
            result = (
                client.create_process_note(
                    item.process_erp_id,
                    item.erp_body,
                )
            )
            note_id = _integer_value(
                _row_value(result, "Id")
            )

            if note_id is None:
                raise RuntimeError(
                    "Wowiport hat keine "
                    "Notiz-ID zurückgegeben."
                )

    except Exception as exc:
        db.session.rollback()

        item = db.session.get(
            ProcessInfo,
            info_id,
        )

        if item is not None:
            item.state = (
                "uncertain"
                if (
                    item.state == "uncertain"
                    or isinstance(
                        exc,
                        requests.RequestException,
                    )
                )
                else "failed"
            )
            item.last_error = str(exc)[:4000]
            item.updated_at = utc_now()
            db.session.commit()

        raise

    item.erp_note_id = note_id
    item.state = "synced"
    item.erp_date = date.today()
    item.mail_state = "pending"
    item.mail_available_at = utc_now()
    item.last_error = None
    item.updated_at = utc_now()

    db.session.commit()
    return item


def mark_info_read(
    item: ProcessInfo,
    user_id: int,
) -> None:
    receipt = db.session.get(
        ProcessInfoRead,
        (item.id, user_id),
    )

    if receipt is None:
        receipt = ProcessInfoRead(
            process_info_id=item.id,
            user_id=user_id,
            read_version=item.version,
        )
        db.session.add(receipt)
    else:
        receipt.read_version = item.version
        receipt.read_at = utc_now()


def mark_process_infos_read(
    process_erp_id: int,
    user_id: int,
    info_ids: list[str],
) -> None:
    if not commissions_for_process(
        process_erp_id,
        user_id,
    ):
        raise ValueError(
            "Der Vorgang ist diesem "
            "Benutzer nicht zugeordnet."
        )

    items = ProcessInfo.query.filter(
        ProcessInfo.id.in_(info_ids),
        ProcessInfo.process_erp_id
        == process_erp_id,
        ProcessInfo.direction
        == OFFICE_DIRECTION,
        ProcessInfo.state == "synced",
    ).all()

    for item in items:
        mark_info_read(
            item,
            user_id,
        )


def is_info_unread(
    info: ProcessInfo,
    user_id: int,
) -> bool:
    if (
        info.direction != OFFICE_DIRECTION
        or info.state != "synced"
    ):
        return False

    receipt = db.session.get(
        ProcessInfoRead,
        (info.id, user_id),
    )

    return (
        receipt is None
        or receipt.read_version
        < info.version
    )


def unread_count_for_user(
    user_id: int,
) -> int:
    process_ids = (
        db.session.query(
            Commission.crafts_process_erp_id
        )
        .filter(
            Commission.user_id == user_id,
            Commission
            .crafts_process_erp_id
            .is_not(None),
            Commission.deleted_at.is_(None),
            Commission.local_status
            != CommissionLocalStatus
            .DELETED.value,
        )
        .distinct()
        .subquery()
    )

    count = (
        db.session.query(
            func.count(
                distinct(ProcessInfo.id)
            )
        )
        .outerjoin(
            ProcessInfoRead,
            and_(
                ProcessInfoRead.process_info_id
                == ProcessInfo.id,
                ProcessInfoRead.user_id
                == user_id,
            ),
        )
        .filter(
            ProcessInfo.process_erp_id.in_(
                db.session.query(
                    process_ids.c
                    .crafts_process_erp_id
                )
            ),
            ProcessInfo.direction
            == OFFICE_DIRECTION,
            ProcessInfo.state == "synced",
            or_(
                ProcessInfoRead
                .process_info_id
                .is_(None),
                ProcessInfoRead.read_version
                < ProcessInfo.version,
            ),
        )
        .scalar()
    )

    return int(count or 0)


def unread_summary_for_user(
    user_id: int,
) -> dict[str, Any]:
    commissions = Commission.query.filter(
        Commission.user_id == user_id,
        Commission
        .crafts_process_erp_id
        .is_not(None),
        Commission.deleted_at.is_(None),
        Commission.local_status
        != CommissionLocalStatus.DELETED.value,
    ).all()

    process_counts: dict[int, int] = {}
    commission_counts: dict[str, int] = {}

    for commission in commissions:
        process_id = (
            commission.crafts_process_erp_id
        )

        if process_id is None:
            continue

        if process_id not in process_counts:
            process_counts[process_id] = (
                _unread_count_for_process(
                    process_id,
                    user_id,
                )
            )

        commission_counts[
            str(commission.id)
        ] = process_counts[process_id]

    return {
        "total": sum(
            process_counts.values()
        ),
        "by_commission": commission_counts,
    }


def _unread_count_for_process(
    process_erp_id: int,
    user_id: int,
) -> int:
    count = (
        db.session.query(
            func.count(ProcessInfo.id)
        )
        .outerjoin(
            ProcessInfoRead,
            and_(
                ProcessInfoRead.process_info_id
                == ProcessInfo.id,
                ProcessInfoRead.user_id
                == user_id,
            ),
        )
        .filter(
            ProcessInfo.process_erp_id
            == process_erp_id,
            ProcessInfo.direction
            == OFFICE_DIRECTION,
            ProcessInfo.state == "synced",
            or_(
                ProcessInfoRead
                .process_info_id
                .is_(None),
                ProcessInfoRead.read_version
                < ProcessInfo.version,
            ),
        )
        .scalar()
    )

    return int(count or 0)


def sync_next_due_process() -> int:
    now = utc_now()

    thread = (
        ProcessInfoThread.query
        .filter(
            ProcessInfoThread.next_sync_at
            <= now
        )
        .order_by(
            ProcessInfoThread
            .next_sync_at
            .asc()
        )
        .first()
    )

    notify = True

    if thread is None:
        process_ids = [
            row[0]
            for row in db.session.query(
                Commission
                .crafts_process_erp_id
            )
            .filter(
                Commission
                .crafts_process_erp_id
                .is_not(None),
                Commission.deleted_at.is_(None),
                Commission.local_status
                != CommissionLocalStatus
                .DELETED.value,
            )
            .distinct()
            .all()
        ]

        existing_ids = {
            row[0]
            for row in db.session.query(
                ProcessInfoThread
                .process_erp_id
            ).all()
        }

        missing_id = next(
            (
                process_id
                for process_id in process_ids
                if process_id
                not in existing_ids
            ),
            None,
        )

        if missing_id is None:
            return 0

        thread = ProcessInfoThread(
            process_erp_id=missing_id
        )
        db.session.add(thread)
        db.session.commit()
        notify = False

    process_id = thread.process_erp_id

    try:
        sync_process_infos(
            process_id,
            notify=notify,
        )
        db.session.commit()

    except Exception as exc:
        db.session.rollback()

        thread = db.session.get(
            ProcessInfoThread,
            process_id,
        )

        if thread is None:
            thread = ProcessInfoThread(
                process_erp_id=process_id
            )
            db.session.add(thread)

        thread.next_sync_at = (
            utc_now()
            + timedelta(minutes=5)
        )
        thread.last_error = str(exc)[:4000]
        thread.updated_at = utc_now()

        db.session.commit()

        current_app.logger.exception(
            "process_info_sync_failed "
            "process_erp_id=%s",
            process_id,
        )

    return 1


def process_one_info_email() -> int:
    now = utc_now()

    ProcessInfo.query.filter(
        ProcessInfo.mail_state == "sending",
        ProcessInfo.updated_at
        < now - timedelta(minutes=15),
    ).update(
        {
            ProcessInfo.mail_state: "failed",
            ProcessInfo.mail_available_at: now,
        },
        synchronize_session=False,
    )
    db.session.commit()

    item = (
        ProcessInfo.query.filter(
            ProcessInfo.direction
            == CRAFTSMAN_DIRECTION,
            ProcessInfo.state == "synced",
            ProcessInfo.mail_state.in_(
                ("pending", "failed")
            ),
            ProcessInfo.mail_attempt_count < 5,
            ProcessInfo.mail_available_at
            <= now,
        )
        .order_by(
            ProcessInfo
            .mail_available_at
            .asc(),
            ProcessInfo.created_at.asc(),
        )
        .with_for_update(
            skip_locked=True
        )
        .first()
    )

    if item is None:
        db.session.rollback()
        return 0

    item.mail_state = "sending"
    item.mail_attempt_count += 1
    info_id = item.id

    db.session.commit()

    try:
        recipients = (
            _responsible_official_emails(
                item.process_erp_id
            )
        )

        if not recipients:
            raise RuntimeError(
                "Für den Vorgang wurde "
                "keine E-Mail-Adresse gefunden."
            )

        _send_process_info_email(
            item,
            recipients,
        )

    except Exception as exc:
        db.session.rollback()

        item = db.session.get(
            ProcessInfo,
            info_id,
        )

        if item is not None:
            delay = min(
                60 * (
                    2 ** max(
                        item.mail_attempt_count - 1,
                        0,
                    )
                ),
                3600,
            )
            item.mail_state = "failed"
            item.mail_available_at = (
                utc_now()
                + timedelta(seconds=delay)
            )
            item.last_error = str(exc)[:4000]
            item.updated_at = utc_now()

            db.session.commit()

        current_app.logger.exception(
            "process_info_email_failed "
            "info_id=%s",
            info_id,
        )

        return 1

    item.mail_state = "sent"
    item.mail_recipients = recipients
    item.mail_available_at = None
    item.last_error = None
    item.updated_at = utc_now()

    db.session.commit()
    return 1


def process_process_info_work() -> int:
    return (
        process_one_info_email()
        + sync_next_due_process()
    )


def bootstrap_process_infos() -> tuple[int, int]:
    process_ids = [
        row[0]
        for row in db.session.query(
            Commission.crafts_process_erp_id
        )
        .filter(
            Commission
            .crafts_process_erp_id
            .is_not(None),
            Commission.deleted_at.is_(None),
            Commission.local_status
            != CommissionLocalStatus
            .DELETED.value,
        )
        .distinct()
        .all()
    ]

    changed = 0

    for process_id in process_ids:
        changed += sync_process_infos(
            process_id,
            notify=False,
        )
        db.session.commit()

    return len(process_ids), changed


def _responsible_official_emails(
    process_erp_id: int,
) -> list[str]:
    service = WowiportService()
    official_ids: set[int] = set()

    for commission in commissions_for_process(
        process_erp_id
    ):
        official_id = (
            commission
            .responsible_official_repair_erp_id
        )

        if (
            official_id is None
            and commission.erp_id is not None
        ):
            official_id = (
                service
                .get_commission_responsible_official_id(
                    commission.erp_id
                )
            )

            if official_id is not None:
                commission.responsible_official_repair_erp_id = (
                    official_id
                )

        if official_id is not None:
            official_ids.add(official_id)

    recipients: set[str] = set()

    for official_id in official_ids:
        row = (
            service.get_responsible_official(
                official_id
            )
        )
        person = _row_value(
            row,
            "Person",
        )
        communication = (
            _row_value(
                person,
                "FirstEmailCommunication",
            )
            if isinstance(person, dict)
            else None
        )
        value = (
            _row_value(
                communication,
                "Content",
            )
            if isinstance(
                communication,
                dict,
            )
            else None
        )

        if value:
            email = str(value).strip()
            Address(addr_spec=email)
            recipients.add(email)

    return sorted(recipients)


def _send_process_info_email(
    info: ProcessInfo,
    recipients: list[str],
) -> None:
    smtp_host = settings.get(
        SettingKeys.SMTP_HOST
    ).strip()
    smtp_port = settings.get(
        SettingKeys.SMTP_PORT
    )
    smtp_sender = settings.get(
        SettingKeys.SMTP_SENDER
    ).strip()

    if not smtp_host or not smtp_sender:
        raise RuntimeError(
            "SMTP-Server oder "
            "Absenderadresse fehlen."
        )

    references = [
        (
            commission.erp_idnum
            or commission.code
            or str(commission.id)
        )
        for commission
        in commissions_for_process(
            info.process_erp_id
        )
    ]

    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = (
        "Neue Information zu Vorgang "
        f"{info.process_erp_id}"
    )

    sender_domain = (
        smtp_sender.rsplit("@", 1)[1]
        if "@" in smtp_sender
        else "datlaeuft.local"
    )
    message["Message-ID"] = (
        f"<process-info-{info.id}"
        f"@{sender_domain}>"
    )

    message.set_content(
        "\n".join(
            (
                (
                    "Handwerker: "
                    f"{info.author_name or 'Unbekannt'}"
                ),
                (
                    "Vorgang: "
                    f"{info.process_erp_id}"
                ),
                (
                    "Aufträge: "
                    f"{', '.join(references)}"
                ),
                "",
                info.body,
            )
        )
    )

    send_email(
        message,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )


def _row_value(
    row: Any,
    name: str,
) -> Any:
    if not isinstance(row, dict):
        return None

    target = (
        name.replace("_", "").casefold()
    )

    for key, value in row.items():
        normalized_key = (
            str(key)
            .replace("_", "")
            .casefold()
        )

        if normalized_key == target:
            return value

    return None


def _integer_value(
    value: Any,
) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None

    return result if result > 0 else None


def _date_value(
    value: Any,
) -> date | None:
    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(
            str(value)[:10]
        )
    except (TypeError, ValueError):
        return None


def _note_author(
    row: dict[str, Any],
) -> str:
    user = _row_value(row, "User")

    if isinstance(user, dict):
        value = _row_value(
            user,
            "UserName",
        )

        if value:
            return str(value)

    return "Sachbearbeitung"