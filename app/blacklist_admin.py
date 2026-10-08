from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from .audit import audit
from .auth import admin_required
from .blacklist import blacklist_ids, include_blacklisted
from .extensions import db
from .models import BlacklistEntry, ExternalCraftsman, User, WowiResponsibleOfficial

blacklist_bp = Blueprint("blacklist", __name__)
TARGETS = {
    "user": (User, "Benutzer", "display_name"),
    "official": (WowiResponsibleOfficial, "Sachbearbeiter (Wowiport)", "name"),
    "external": (ExternalCraftsman, "Externe Benutzer", "display_name"),
}


@blacklist_bp.route("/admin/blacklist", methods=["GET", "POST"])
@admin_required
@include_blacklisted()
def blacklist_page():
    actor_id = current_user.id
    if request.method == "POST":
        try:
            target_type, raw_id = request.form.get("target", "").split(":", 1)
            if target_type not in TARGETS or not raw_id.isdigit() or int(raw_id) <= 0:
                raise ValueError("Bitte einen gültigen Eintrag auswählen.")

            target_id = int(raw_id)
            entry = db.session.get(BlacklistEntry, (target_type, target_id))
            action = request.form.get("action")

            if action == "remove":
                if entry is None:
                    raise ValueError("Der Blacklist-Eintrag existiert nicht.")
                db.session.delete(entry)
            elif action == "add":
                model, _, name_field = TARGETS[target_type]
                target = db.session.get(model, target_id)
                if target is None:
                    raise ValueError("Der ausgewählte Eintrag existiert nicht.")
                if entry is not None:
                    raise ValueError("Der Eintrag steht bereits auf der Blacklist.")

                reason = request.form.get("reason", "").strip()
                if len(reason) > 500:
                    raise ValueError("Der Grund darf höchstens 500 Zeichen enthalten.")

                entry = BlacklistEntry(
                    target_type=target_type,
                    target_id=target_id,
                    name_snapshot=(getattr(target, name_field) or str(target_id))[:500],
                    reason=reason or None,
                )
                db.session.add(entry)
            else:
                raise ValueError("Die Aktion ist ungültig.")

            db.session.flush()
            if actor_id in blacklist_ids("user"):
                raise ValueError("Eine Sperre deines eigenen Benutzerzugangs ist nicht möglich.")

            audit(
                f"blacklist.{action}",
                target_type,
                target_id,
                metadata={"name": entry.name_snapshot, "reason": entry.reason},
                actor_user_id=actor_id,
            )
            db.session.commit()
            flash("Blacklist gespeichert.", "success")
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(
                str(exc) if isinstance(exc, ValueError)
                else "Der Eintrag wurde gleichzeitig geändert. Bitte erneut versuchen.",
                "danger",
            )
        return redirect(url_for("blacklist.blacklist_page"))

    entries = BlacklistEntry.query.order_by(BlacklistEntry.name_snapshot.asc()).all()
    choices = {}
    for target_type, (model, label, name_field) in TARGETS.items():
        blocked_ids = blacklist_ids(target_type)
        records = model.query.order_by(getattr(model, name_field).asc()).all()
        choices[target_type] = {
            "label": label,
            "items": [
                {
                    "id": target.erp_id if target_type == "official" else target.id,
                    "name": getattr(target, name_field) or "Ohne Namen",
                }
                for target in records
                if (target.erp_id if target_type == "official" else target.id) not in blocked_ids
                and (target_type != "user" or target.id != actor_id)
            ],
        }

    return render_template(
        "admin/blacklist.html", entries=entries, choices=choices, targets=TARGETS,
    )