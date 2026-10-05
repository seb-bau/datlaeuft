from __future__ import annotations

import base64
from hashlib import sha256
from urllib.parse import urlsplit

import click
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect,
    request, send_from_directory, session, url_for,
)
from flask_login import current_user

from .audit import audit
from .auth import foreman_required
from .extensions import db
from .models import (
    BrowserPushSubscription, Commission, ProcessInfo, User, utc_now,
)
from .process_info_notifications import (
    office_user, read_receipts, record_visible_reads, visible_items,
)
from .settings import SettingKeys, settings


process_info_web_bp = Blueprint(
    "process_info_web", __name__, cli_group="desktop-push",
)


def chat_status(process_id: int | None):
    messages = ProcessInfo.query.filter(
        ProcessInfo.process_erp_id == process_id,
        ProcessInfo.state != "deleted",
    ).order_by(ProcessInfo.created_at.asc()).all()

    return jsonify({
        "items": [
            {
                "id": message.id,
                "version": message.version,
                "read_receipts": read_receipts(message),
            }
            for message in messages
        ],
    })


@process_info_web_bp.get("/push-worker.js")
def push_worker():
    response = send_from_directory(
        current_app.static_folder, "js/push-worker.js",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@process_info_web_bp.get("/admin/web-push/config")
@foreman_required
def push_config():
    if not office_user(current_user):
        abort(403)

    public_key = settings.get(SettingKeys.WEB_PUSH_VAPID_PUBLIC_KEY)
    return jsonify({
        "enabled": bool(
            settings.get(SettingKeys.WEB_PUSH_ENABLED)
            and public_key
            and settings.get(SettingKeys.WEB_PUSH_VAPID_PRIVATE_KEY)
            and settings.get(SettingKeys.WEB_PUSH_VAPID_SUBJECT)
        ),
        "public_key": public_key,
    })


def normalized_subscription(data: object) -> dict:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("keys"), dict)
    ):
        raise ValueError("Das Push-Abonnement ist ungültig.")

    endpoint = str(data.get("endpoint") or "")
    url = urlsplit(endpoint)
    host = url.hostname or ""
    known_host = host in {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "updates.autopush.prod.mozaws.net",
        "web.push.apple.com",
    } or host.endswith(".notify.windows.com")

    if (
        not known_host
        or url.scheme != "https"
        or url.port not in (None, 443)
        or url.username
        or url.password
        or url.fragment
        or len(endpoint) > 4096
    ):
        raise ValueError("Der Push-Dienst wird nicht unterstützt.")

    keys = {}
    for name, length in (("p256dh", 65), ("auth", 16)):
        value = data["keys"].get(name)
        if not isinstance(value, str) or len(value) > 128:
            raise ValueError("Der Push-Schlüssel ist ungültig.")

        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) != length:
            raise ValueError(
                "Der Push-Schlüssel hat eine ungültige Länge."
            )

        if name == "p256dh":
            ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), raw,
            )

        keys[name] = value

    return {"endpoint": endpoint, "keys": keys}


@process_info_web_bp.post("/admin/web-push/subscription")
@foreman_required
def save_subscription():
    if not office_user(current_user):
        abort(403)

    try:
        data = normalized_subscription(
            request.get_json(silent=True)
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"message": str(exc)}), 400

    endpoint_hash = sha256(data["endpoint"].encode()).hexdigest()
    subscription = BrowserPushSubscription.query.filter_by(
        endpoint_hash=endpoint_hash,
    ).one_or_none()

    if subscription is None:
        subscription = BrowserPushSubscription(
            endpoint_hash=endpoint_hash,
        )
        db.session.add(subscription)

    subscription.user_id = current_user.id
    subscription.subscription = data
    subscription.active = True
    db.session.flush()
    session["web_push_subscription_id"] = subscription.id
    db.session.commit()

    return jsonify({
        "message": "Desktop-Benachrichtigungen sind aktiviert.",
    })


@process_info_web_bp.delete("/admin/web-push/subscription")
@foreman_required
def remove_subscription():
    subscription = db.session.get(
        BrowserPushSubscription,
        session.get("web_push_subscription_id"),
    )
    if subscription and subscription.user_id == current_user.id:
        subscription.active = False

    session.pop("web_push_subscription_id", None)
    db.session.commit()

    return jsonify({
        "message": "Desktop-Benachrichtigungen sind deaktiviert.",
    })


@process_info_web_bp.get(
    "/admin/commissions/<int:commission_id>/infos/status"
)
@foreman_required
def office_chat_status(commission_id: int):
    if not office_user(current_user):
        abort(403)

    commission = db.get_or_404(Commission, commission_id)
    return chat_status(commission.crafts_process_erp_id)


@process_info_web_bp.post(
    "/admin/commissions/<int:commission_id>/infos/read"
)
@foreman_required
def office_chat_read(commission_id: int):
    if not office_user(current_user):
        abort(403)

    commission = db.get_or_404(Commission, commission_id)
    try:
        record_visible_reads(
            commission.crafts_process_erp_id,
            visible_items(request.get_json(silent=True)),
            "craftsman",
            user_id=current_user.id,
        )
        db.session.commit()

    except ValueError as exc:
        db.session.rollback()
        return jsonify({"message": str(exc)}), 400

    return jsonify({"message": "Lesestatus gespeichert."})


@process_info_web_bp.post(
    "/admin/commissions/<int:commission_id>/local-official"
)
@foreman_required
def local_official(commission_id: int):
    if not office_user(current_user):
        abort(403)

    commission = db.get_or_404(Commission, commission_id)
    previous_id = commission.local_official_user_id

    try:
        raw_id = request.form.get(
            "local_official_user_id", "",
        ).strip()
        user_id = int(raw_id) if raw_id else None

        if (
            user_id is not None
            and not office_user(db.session.get(User, user_id))
        ):
            raise ValueError(
                "Bitte einen verfügbaren Sachbearbeiter auswählen."
            )

        commission.local_official_user_id = user_id

        if previous_id != user_id:
            messages = ProcessInfo.query.filter(
                ProcessInfo.process_erp_id
                == commission.crafts_process_erp_id,
                ProcessInfo.direction == "craftsman",
                ProcessInfo.state == "synced",
                ProcessInfo.mail_state.in_(
                    ("pending", "failed", "queued")
                ),
            ).order_by(
                ProcessInfo.id.asc()
            ).with_for_update().all()

            for message in messages:
                message.mail_state = "pending"
                message.mail_attempt_count = 0
                message.mail_available_at = utc_now()

            audit(
                "commission.local_official.update",
                "commission",
                commission.id,
                {
                    "previous_user_id": previous_id,
                    "user_id": user_id,
                },
            )

        db.session.commit()
        flash(
            "Die lokale Sachbearbeitung wurde gespeichert.",
            "success",
        )

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(
        url_for(
            "admin.commission_detail",
            commission_id=commission.id,
        ) + "#sachbearbeitung"
    )


@process_info_web_bp.cli.command("generate-keys")
def generate_keys():
    if settings.get(SettingKeys.WEB_PUSH_VAPID_PRIVATE_KEY):
        raise click.ClickException(
            "Es sind bereits Web-Push-Schlüssel hinterlegt."
        )

    sender = settings.get(SettingKeys.SMTP_SENDER).strip()
    if not sender:
        raise click.ClickException(
            "Bitte zuerst die SMTP-Absenderadresse hinterlegen."
        )

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_data = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public_data = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )

    settings.set(
        SettingKeys.WEB_PUSH_VAPID_PRIVATE_KEY,
        base64.b64encode(private_data).decode(),
    )
    settings.set(
        SettingKeys.WEB_PUSH_VAPID_PUBLIC_KEY,
        base64.urlsafe_b64encode(public_data).decode().rstrip("="),
    )
    settings.set(
        SettingKeys.WEB_PUSH_VAPID_SUBJECT,
        "mailto:" + sender,
    )
    settings.set(SettingKeys.WEB_PUSH_ENABLED, True)
    db.session.commit()
    click.echo("Web-Push-Schlüssel wurden angelegt.")