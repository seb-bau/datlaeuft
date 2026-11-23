from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from .extensions import db
from .models import MobileSyncReport, User, utc_now


def report_text(value, limit):
    if not isinstance(value, str):
        raise ValueError
    return "".join(character for character in value.partition("\n")[0].strip() if character.isprintable())[:limit]


def report_entry(entry):
    if not isinstance(entry, dict) or entry.get("module") not in ("commissions", "inspections"):
        raise ValueError
    title = report_text(entry.get("title"), 120)
    if not title:
        raise ValueError

    entity_id = entry.get("entity_id")
    if entity_id is not None and (type(entity_id) is not int or not 0 < entity_id < 2**31):
        raise ValueError
    created_at = entry.get("created_at")
    if created_at is not None:
        if not isinstance(created_at, str) or len(created_at) > 40:
            raise ValueError
        timestamp = datetime.fromisoformat(created_at)
        if timestamp.tzinfo is None:
            raise ValueError
        created_at = timestamp.astimezone(timezone.utc).isoformat()

    error = report_text(entry["error"], 500) if entry.get("error") is not None else ""
    return {
        "module": entry["module"],
        "title": title,
        "entity_id": entity_id,
        "created_at": created_at,
        "error": error,
        "error_code": report_text(entry["error_code"], 64) if error and entry.get("error_code") is not None else "",
    }


def store_mobile_sync_report(user_id, data):
    if not isinstance(data, dict):
        raise ValueError
    device_id = data.get("device_id")
    if not isinstance(device_id, str) or len(device_id) != 36:
        raise ValueError
    device_id = str(UUID(device_id))
    app_version = report_text(data.get("app_version"), 40)
    app_build = report_text(data.get("app_build"), 40)
    submitted = data.get("entries")
    total_entries = data.get("total_entries")
    if (
        not isinstance(submitted, list) or len(submitted) > 100
        or type(total_entries) is not int or not len(submitted) <= total_entries < 2**31
        or (not submitted and total_entries)
    ):
        raise ValueError
    entries = [report_entry(entry) for entry in submitted]

    db.session.execute(select(User.id).where(User.id == user_id).with_for_update()).scalar_one()
    report = db.session.get(MobileSyncReport, (user_id, device_id), populate_existing=True)
    if entries:
        if report is None:
            report = MobileSyncReport(user_id=user_id, device_id=device_id)
            db.session.add(report)
        report.app_version = app_version
        report.app_build = app_build
        report.reported_at = utc_now()
        report.total_entries = total_entries
        report.entries = entries
    elif report is not None:
        db.session.delete(report)
    db.session.commit()
