from __future__ import annotations

HEALTH_CHANNELS = (
    ("status_code", "Gesamtzustand", None),
    ("worker_age_seconds", "Worker-Alter", (3, 10)),
    ("webhooks_unprocessed", "Webhooks offen", (None, None)),
    ("webhooks_failed", "Webhooks fehlerhaft", (0, None)),
    ("webhooks_oldest_seconds", "Ältester Webhook", (15, 60)),
    ("erp_pending", "ERP-Aufträge offen", (None, None)),
    ("erp_processing", "ERP-Aufträge in Arbeit", (None, None)),
    ("erp_failed", "ERP-Aufträge fehlerhaft", (0, None)),
    ("erp_oldest_seconds", "Ältester ERP-Auftrag", (30, 120)),
    ("synchronizations_failed", "Synchronisierungen fehlerhaft", (0, None)),
    ("mobile_sync_users", "Benutzer mit App-Synchronisierungsproblemen", (0, None)),
    ("commissions_failed", "Auftragsabgleich fehlerhaft", (None, None)),
    ("documents_failed", "Dokumentabgleich fehlerhaft", (None, None)),
    ("media_failed", "Medienabgleich fehlerhaft", (None, None)),
    ("dms_enabled", "DMS aktiviert", None),
    ("documents_in_queue", "Dokumente in Warteschlange", (None, None)),
    ("dms_pending", "DMS ausstehend", (None, None)),
    ("dms_processing", "DMS in Arbeit", (None, None)),
    ("dms_failed", "DMS Wiederholungen", (None, None)),
    ("dms_uncertain", "DMS unklare Uploads", (None, None)),
    ("dms_blocked", "DMS blockiert", (None, 0)),
    ("dms_upload_errors", "DMS-Uploadfehler", (0, None)),
    ("dms_oldest_seconds", "Ältestes DMS-Dokument", (60, 120)),
    ("push_pending", "Push ausstehend", (None, None)),
    ("push_failed", "Push fehlerhaft", (0, None)),
    ("push_discarded", "Push verworfen", (None, None)),
    ("tenant_mail_failed", "Mieter-Mails fehlerhaft", (0, None)),
    ("completion_mail_failed", "Abschluss-Mails fehlerhaft", (0, None)),
    ("external_mail_failed", "Handwerker-Mails fehlerhaft", (0, None)),
    ("process_delivery_failed", "Vorgangs-Meldungen fehlerhaft", (0, None)),
    ("process_sync_failed", "Vorgangsabgleich fehlerhaft", (0, None)),
    ("ticket_ai_failed", "Ticketanalyse fehlerhaft", (0, None)),
    ("safety_archive_failed", "Prüfarchive fehlerhaft", (0, None)),
)

CHANNELS = tuple((key, label) for key, label, limits in HEALTH_CHANNELS)

DEFAULT_HEALTH_RULES = {
    key: {
        "enabled": any(limit is not None for limit in limits),
        "warning": limits[0],
        "error": limits[1],
    }
    for key, label, limits in HEALTH_CHANNELS
    if limits is not None
}


def normalize_health_rules(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_HEALTH_RULES):
        raise ValueError("Die Health-Konfiguration enthält ungültige Prüfungen.")
    rules = {}
    for key, label, limits in HEALTH_CHANNELS:
        if limits is None:
            continue
        supplied = value.get(key, {})
        if not isinstance(supplied, dict) or set(supplied) - {"enabled", "warning", "error"}:
            raise ValueError(f"{label}: Ungültige Health-Konfiguration.")
        rule = {**DEFAULT_HEALTH_RULES[key], **supplied}
        if type(rule["enabled"]) is not bool:
            raise ValueError(f"{label}: Bitte den Prüfstatus auswählen.")
        for name in ("warning", "error"):
            limit = rule[name]
            if limit is not None and (type(limit) is not int or limit < 0):
                raise ValueError(f"{label}: Grenzen müssen nichtnegative ganze Zahlen sein.")
        if rule["enabled"] and rule["warning"] is None and rule["error"] is None:
            raise ValueError(f"{label}: Für eine aktive Prüfung mindestens eine Grenze angeben.")
        if rule["warning"] is not None and rule["error"] is not None:
            if rule["error"] <= rule["warning"]:
                raise ValueError(f"{label}: Die Störungsgrenze muss höher als die Warnungsgrenze sein.")
        rules[key] = rule
    return rules


def evaluate_health_metrics(metrics, configured_rules):
    rules = normalize_health_rules(configured_rules)
    checks = []
    for key, label, limits in HEALTH_CHANNELS:
        if limits is None or key not in metrics:
            continue
        rule = rules[key]
        value = metrics[key]
        timed = key.endswith("_seconds")
        scale = 60 if timed else 1
        value_text = (
            "Keine Rückmeldung" if value < 0
            else f"{value // 60} Min. {value % 60} Sek." if timed
            else str(value)
        )
        paused = (
            key.startswith("dms_")
            or key in {"documents_in_queue", "safety_archive_failed"}
        ) and not metrics.get("dms_enabled", 0)
        active = rule["enabled"] and not paused
        status_code = 0
        reason = ""
        if active:
            if value < 0:
                status_code = 2 if rule["error"] is not None else 1
                reason = f"{label}: Keine Rückmeldung gespeichert."
            else:
                for name, severity, limit_label in (
                    ("error", 2, "Störungsgrenze"),
                    ("warning", 1, "Warnungsgrenze"),
                ):
                    limit = rule[name]
                    if limit is not None and value > limit * scale:
                        status_code = severity
                        unit = " Min." if timed else ""
                        reason = f"{label}: {value_text} ({limit_label}: > {limit}{unit})."
                        break
        else:
            reason = "DMS ist deaktiviert." if paused else "Prüfung ist deaktiviert."
        checks.append({
            "key": key,
            "label": label,
            "value": value,
            "value_text": value_text,
            "unit": "Minuten" if timed else "Anzahl",
            **rule,
            "active": active,
            "status_code": status_code,
            "status": (
                ("In Ordnung", "Warnung", "Störung")[status_code] if active
                else "DMS deaktiviert" if paused else "Deaktiviert"
            ),
            "color": ("success", "warning", "danger")[status_code] if active else "secondary",
            "reason": reason,
        })
    issues = [
        check["reason"]
        for check in sorted(checks, key=lambda check: check["status_code"], reverse=True)
        if check["status_code"]
    ]
    status_code = max((check["status_code"] for check in checks), default=0)
    status = ("In Ordnung", "Warnung", "Störung")[status_code]
    return {
        "checks": checks,
        "issues": issues,
        "status_code": status_code,
        "status": status,
        "status_message": status + (": " + " | ".join(issues) if issues else ""),
    }


def health_sensor_text(metrics):
    message = metrics["status_message"].replace("#", "Nr.")
    if len(message) <= 2000:
        return message
    suffix = f" … ({len(metrics['issues'])} Meldungen; vollständig auf der Health-Seite)"
    return message[:2000 - len(suffix)] + suffix