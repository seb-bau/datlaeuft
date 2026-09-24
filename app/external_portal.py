from __future__ import annotations

from datetime import timedelta
from functools import wraps
from typing import Any, Callable, TypeVar

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import logout_user
from sqlalchemy import and_, or_
from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)

from .attachment_service import (
    commission_attachment_path,
)
from .audit import audit
from .commission_workflow_service import (
    CommissionWorkflowError,
    accept_commission,
    complete_commission,
    reject_commission,
)
from .extensions import db
from .external_craftsman_service import (
    as_utc,
    external_token_is_current,
    hash_external_token,
    validate_external_password,
)
from .media_sync import MEDIA_SOURCE
from .models import (
    Commission,
    CommissionAttachment,
    CommissionLocalStatus,
    CommissionPhoto,
    ExternalCraftsman,
    ExternalCraftsmanAccessToken,
    ProcessInfo,
    utc_now,
)
from .photo_service import commission_photo_path
from .process_info_service import (
    create_external_outgoing_info,
    send_commission_status_email,
    sync_process_infos,
    write_outgoing_info,
)
from .wowiport import WowiportService
from uuid import uuid4


F = TypeVar("F", bound=Callable[..., Any])
external_portal_bp = Blueprint(
    "external_portal",
    __name__,
    url_prefix="/auftragnehmer",
)
DUMMY_PASSWORD_HASH = generate_password_hash(
    "external-login-dummy-password"
)
ACTIVE_STATUSES = (
    CommissionLocalStatus.OPEN.value,
    CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value,
    CommissionLocalStatus.IN_PROGRESS.value,
)
COMPLETED_STATUSES = (
    CommissionLocalStatus.COMPLETION_REVIEW.value,
    CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
    CommissionLocalStatus.DONE.value,
)
STATUS_LABELS = {
    CommissionLocalStatus.RECORDED.value: "Erfasst",
    CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value:
        "Wartet auf Annahme",
    CommissionLocalStatus.OPEN.value: "Offen",
    CommissionLocalStatus.IN_PROGRESS.value:
        "In Bearbeitung",
    CommissionLocalStatus.COMPLETION_REVIEW.value:
        "Abschluss wird geprüft",
    CommissionLocalStatus.TENANT_CHARGE_REVIEW.value:
        "Abrechnung wird geprüft",
    CommissionLocalStatus.DONE.value: "Abgeschlossen",
}


def current_external_craftsman(
) -> ExternalCraftsman | None:
    cached = getattr(
        g,
        "external_craftsman",
        None,
    )

    if cached is not None:
        return cached

    craftsman_id = session.get(
        "external_craftsman_id"
    )

    if not isinstance(craftsman_id, int):
        return None

    craftsman = db.session.get(
        ExternalCraftsman,
        craftsman_id,
    )

    if (
        craftsman is None
        or not craftsman.portal_enabled
        or not craftsman.wowi_active
        or not craftsman.password_hash
    ):
        clear_external_session()
        return None

    g.external_craftsman = craftsman
    return craftsman


def external_craftsman_required(view: F) -> F:
    @wraps(view)
    def wrapped(
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        craftsman = current_external_craftsman()

        if craftsman is None:
            next_url = request.full_path.rstrip("?")

            if (
                next_url.startswith("/auftragnehmer/")
                and not next_url.startswith("//")
            ):
                session["external_next"] = next_url

            return redirect(
                url_for("external_portal.login")
            )

        return view(*args, **kwargs)

    return wrapped


def clear_external_session() -> None:
    session.pop("external_craftsman_id", None)
    session.pop(
        "external_craftsman_authenticated_at",
        None,
    )
    session.pop("external_next", None)


@external_portal_bp.context_processor
def external_portal_context() -> dict[str, Any]:
    return {
        "external_craftsman":
            current_external_craftsman(),
        "external_status_labels": STATUS_LABELS,
    }


@external_portal_bp.after_request
def protect_external_response(
    response: Response,
) -> Response:
    response.headers["Cache-Control"] = (
        "no-store, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = (
        "nosniff"
    )
    response.headers["Referrer-Policy"] = (
        "same-origin"
    )
    return response


@external_portal_bp.route(
    "/login",
    methods=["GET", "POST"],
)
def login() -> Any:
    if current_external_craftsman() is not None:
        return redirect(
            url_for(
                "external_portal.commissions"
            )
        )

    if request.method == "POST":
        username = str(
            request.form.get("username") or ""
        ).strip().casefold()
        password = str(
            request.form.get("password") or ""
        )
        craftsman = (
            ExternalCraftsman.query
            .filter_by(username=username)
            .one_or_none()
        )
        now = utc_now()
        locked = bool(
            craftsman is not None
            and craftsman.locked_until is not None
            and as_utc(craftsman.locked_until) > now
        )
        password_matches = (
            craftsman.check_password(password)
            if craftsman is not None and not locked
            else check_password_hash(
                DUMMY_PASSWORD_HASH,
                password,
            )
        )
        login_allowed = bool(
            craftsman is not None
            and not locked
            and craftsman.portal_enabled
            and craftsman.wowi_active
            and password_matches
        )

        if login_allowed and craftsman is not None:
            craftsman.failed_login_count = 0
            craftsman.locked_until = None
            craftsman.last_login_at = now
            next_url = session.get(
                "external_next"
            )
            logout_user()
            session.clear()
            session["external_craftsman_id"] = (
                craftsman.id
            )
            session[
                "external_craftsman_authenticated_at"
            ] = now.isoformat()
            audit(
                "external.auth.login",
                "external_craftsman",
                craftsman.id,
                actor_external_craftsman_id=(
                    craftsman.id
                ),
            )
            db.session.commit()

            if (
                not isinstance(next_url, str)
                or not next_url.startswith(
                    "/auftragnehmer/"
                )
                or next_url.startswith("//")
            ):
                next_url = url_for(
                    "external_portal.commissions"
                )

            return redirect(next_url)

        if craftsman is not None and not locked:
            craftsman.failed_login_count += 1

            if craftsman.failed_login_count >= 5:
                craftsman.failed_login_count = 0
                craftsman.locked_until = (
                    now + timedelta(minutes=15)
                )

            db.session.commit()

        flash(
            "Benutzername oder Kennwort ist ungültig.",
            "danger",
        )

    return render_template(
        "external_portal/login.html"
    )


@external_portal_bp.post("/logout")
@external_craftsman_required
def logout() -> Any:
    craftsman = current_external_craftsman()
    audit(
        "external.auth.logout",
        "external_craftsman",
        craftsman.id,
        actor_external_craftsman_id=craftsman.id,
    )
    db.session.commit()
    clear_external_session()
    return redirect(
        url_for("external_portal.login")
    )


@external_portal_bp.route(
    "/einladung/<string:raw_token>",
    methods=["GET", "POST"],
)
def invitation(raw_token: str) -> Any:
    token_query = (
        ExternalCraftsmanAccessToken.query
        .filter_by(
            token_hash=hash_external_token(
                raw_token
            ),
            purpose="invitation",
        )
    )
    token = token_query.one_or_none()
    valid = bool(
        token is not None
        and external_token_is_current(token)
        and token.external_craftsman.portal_enabled
        and token.external_craftsman.wowi_active
    )

    if request.method == "POST" and valid:
        token = token_query.with_for_update().one()

        if not external_token_is_current(token):
            valid = False
        else:
            password = str(
                request.form.get("password") or ""
            )
            password_confirmation = str(
                request.form.get(
                    "password_confirmation"
                )
                or ""
            )
            password_error = (
                validate_external_password(password)
            )

            if password_error:
                flash(password_error, "danger")
            elif password != password_confirmation:
                flash(
                    "Die Kennwörter stimmen nicht überein.",
                    "danger",
                )
            else:
                craftsman = (
                    token.external_craftsman
                )
                craftsman.set_password(password)
                craftsman.failed_login_count = 0
                craftsman.locked_until = None
                token.used_at = utc_now()
                audit(
                    "external.auth.invitation_accepted",
                    "external_craftsman",
                    craftsman.id,
                    actor_external_craftsman_id=(
                        craftsman.id
                    ),
                )
                db.session.commit()
                logout_user()
                session.clear()
                session[
                    "external_craftsman_id"
                ] = craftsman.id
                session[
                    "external_craftsman_authenticated_at"
                ] = utc_now().isoformat()
                flash(
                    "Ihr Kennwort wurde gespeichert.",
                    "success",
                )
                return redirect(
                    url_for(
                        "external_portal.commissions"
                    )
                )

    return render_template(
        "external_portal/invitation.html",
        token=token,
        valid=valid,
    )


@external_portal_bp.route(
    "/kennwort",
    methods=["GET", "POST"],
)
@external_craftsman_required
def change_password() -> Any:
    craftsman = current_external_craftsman()

    if request.method == "POST":
        current_password = str(
            request.form.get("current_password")
            or ""
        )
        password = str(
            request.form.get("password") or ""
        )
        password_confirmation = str(
            request.form.get(
                "password_confirmation"
            )
            or ""
        )
        password_error = (
            validate_external_password(password)
        )

        if not craftsman.check_password(
            current_password
        ):
            flash(
                "Das bisherige Kennwort ist falsch.",
                "danger",
            )
        elif password_error:
            flash(password_error, "danger")
        elif password != password_confirmation:
            flash(
                "Die Kennwörter stimmen nicht überein.",
                "danger",
            )
        else:
            craftsman.set_password(password)
            audit(
                "external.auth.password_changed",
                "external_craftsman",
                craftsman.id,
                actor_external_craftsman_id=(
                    craftsman.id
                ),
            )
            db.session.commit()
            flash(
                "Ihr Kennwort wurde geändert.",
                "success",
            )
            return redirect(
                url_for(
                    "external_portal.commissions"
                )
            )

    return render_template(
        "external_portal/change_password.html"
    )


@external_portal_bp.get("/")
@external_craftsman_required
def index() -> Any:
    return redirect(
        url_for("external_portal.commissions")
    )


@external_portal_bp.get("/auftraege")
@external_craftsman_required
def commissions() -> Any:
    craftsman = current_external_craftsman()
    scope = str(
        request.args.get("status") or "aktiv"
    ).strip().lower()

    if scope not in {
        "aktiv",
        "abgeschlossen",
        "alle",
    }:
        scope = "aktiv"

    query = Commission.query.filter(
        Commission.craftsman_erp_id
        == craftsman.craftsman_erp_id,
        Commission.deleted_at.is_(None),
        Commission.local_status
        != CommissionLocalStatus.DELETED.value,
    )

    if scope == "aktiv":
        query = query.filter(
            Commission.local_status.in_(
                ACTIVE_STATUSES
            )
        )
    elif scope == "abgeschlossen":
        query = query.filter(
            Commission.local_status.in_(
                COMPLETED_STATUSES
            )
        )

    items = query.order_by(
        Commission.is_high_priority.desc(),
        Commission.execution_from.asc(),
        Commission.id.desc(),
    ).all()

    return render_template(
        "external_portal/commissions.html",
        items=items,
        scope=scope,
    )


@external_portal_bp.get(
    "/auftraege/<string:public_id>"
)
@external_craftsman_required
def commission_detail(public_id: str) -> Any:
    commission = external_commission_or_404(
        public_id
    )
    contact_items: list[dict[str, Any]] = []
    infos: list[ProcessInfo] = []
    info_sync_error = None

    if commission.erp_id is not None:
        try:
            contact_items = (
                WowiportService()
                .get_commission_contact_items(
                    commission.erp_id
                )
            )
        except Exception:
            current_app.logger.exception(
                "external_commission_contacts_failed "
                "commission_id=%s",
                commission.id,
            )

    if (
        commission.crafts_process_erp_id
        is not None
    ):
        try:
            sync_process_infos(
                commission.crafts_process_erp_id,
                notify=True,
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            info_sync_error = (
                "Die Zusatzinformationen konnten "
                "momentan nicht aktualisiert werden."
            )
            current_app.logger.exception(
                "external_process_info_sync_failed "
                "commission_id=%s process_erp_id=%s",
                commission.id,
                commission.crafts_process_erp_id,
            )

        infos = (
            ProcessInfo.query
            .filter(
                ProcessInfo.process_erp_id
                == commission.crafts_process_erp_id,
                ProcessInfo.state != "deleted",
            )
            .order_by(
                ProcessInfo.erp_date
                .is_(None)
                .asc(),
                ProcessInfo.erp_date.asc(),
                ProcessInfo.created_at.asc(),
            )
            .all()
        )

    attachments = (
        CommissionAttachment.query
        .filter(
            CommissionAttachment.commission_id
            == commission.id,
            CommissionAttachment.active.is_(True),
        )
        .order_by(
            CommissionAttachment.created_at.desc(),
            CommissionAttachment.id.desc(),
        )
        .all()
    )

    return render_template(
        "external_portal/commission_detail.html",
        item=commission,
        contact_items=contact_items,
        attachments=attachments,
        photos=external_commission_photos(
            commission
        ),
        infos=infos,
        info_sync_error=info_sync_error,
        info_request_id=str(uuid4()),
    )


@external_portal_bp.post(
    "/auftraege/<string:public_id>/informationen"
)
@external_craftsman_required
def commission_info_create(
    public_id: str,
) -> Any:
    craftsman = current_external_craftsman()
    commission = external_commission_or_404(
        public_id
    )
    ensure_external_assignment(
        commission,
        craftsman,
    )

    try:
        info = create_external_outgoing_info(
            commission=commission,
            external_craftsman=craftsman,
            request_id=str(
                request.form.get(
                    "request_id"
                )
                or ""
            ),
            body=str(
                request.form.get("body")
                or ""
            ),
        )

        if info.state != "synced":
            info = write_outgoing_info(
                info.id
            )

        audit(
            "external.process_info.create",
            "process_info",
            info.id,
            {
                "commission_id": commission.id,
                "process_erp_id": (
                    info.process_erp_id
                ),
            },
            actor_external_craftsman_id=(
                craftsman.id
            ),
        )
        db.session.commit()
        flash(
            "Die Zusatzinformation wurde übermittelt.",
            "success",
        )

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "external_process_info_create_failed "
            "commission_id=%s",
            commission.id,
        )
        flash(
            "Die Zusatzinformation konnte nicht "
            "übertragen werden. Sie können die "
            "Übertragung erneut versuchen.",
            "danger",
        )

    target_url = url_for(
        "external_portal.commission_detail",
        public_id=public_id,
    )

    return redirect(
        f"{target_url}#zusatzinformationen"
    )


@external_portal_bp.post(
    "/auftraege/<string:public_id>/"
    "informationen/<string:info_id>/erneut-senden"
)
@external_craftsman_required
def commission_info_retry(
    public_id: str,
    info_id: str,
) -> Any:
    craftsman = current_external_craftsman()
    commission = external_commission_or_404(
        public_id
    )
    ensure_external_assignment(
        commission,
        craftsman,
    )

    info = ProcessInfo.query.filter(
        ProcessInfo.id == info_id,
        ProcessInfo.process_erp_id
        == commission.crafts_process_erp_id,
        ProcessInfo.sender_external_craftsman_id
        == craftsman.id,
        ProcessInfo.state != "deleted",
    ).one_or_none()

    if info is None:
        abort(404)

    try:
        if info.state != "synced":
            write_outgoing_info(
                info.id
            )

        audit(
            "external.process_info.retry",
            "process_info",
            info.id,
            {
                "commission_id": commission.id,
                "process_erp_id": (
                    info.process_erp_id
                ),
            },
            actor_external_craftsman_id=(
                craftsman.id
            ),
        )
        db.session.commit()
        flash(
            "Die Zusatzinformation wurde übermittelt.",
            "success",
        )

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "external_process_info_retry_failed "
            "commission_id=%s info_id=%s",
            commission.id,
            info.id,
        )
        flash(
            "Die Zusatzinformation konnte erneut "
            "nicht übertragen werden.",
            "danger",
        )

    target_url = url_for(
        "external_portal.commission_detail",
        public_id=public_id,
    )

    return redirect(
        f"{target_url}#zusatzinformationen"
    )


@external_portal_bp.post(
    "/auftraege/<string:public_id>/annehmen"
)
@external_craftsman_required
def commission_accept(public_id: str) -> Any:
    craftsman = current_external_craftsman()
    commission = external_commission_or_404(
        public_id
    )
    db.session.refresh(
        commission,
        with_for_update=True,
    )
    ensure_external_assignment(
        commission,
        craftsman,
    )

    try:
        accept_commission(
            commission,
            external_craftsman=craftsman,
        )
        db.session.commit()
        flash(
            "Der Auftrag wurde angenommen.",
            "success",
        )
    except CommissionWorkflowError as exc:
        db.session.rollback()
        flash(exc.message, "danger")

    return redirect(
        url_for(
            "external_portal.commission_detail",
            public_id=public_id,
        )
    )


@external_portal_bp.post(
    "/auftraege/<string:public_id>/ablehnen"
)
@external_craftsman_required
def commission_reject(public_id: str) -> Any:
    craftsman = current_external_craftsman()
    commission = external_commission_or_404(
        public_id
    )
    db.session.refresh(
        commission,
        with_for_update=True,
    )
    ensure_external_assignment(
        commission,
        craftsman,
    )

    try:
        reason = reject_commission(
            commission,
            str(request.form.get("reason") or ""),
            external_craftsman=craftsman,
        )
        db.session.commit()
        send_external_status_notification(
            commission,
            event_name="Auftrag abgelehnt",
            reason=reason,
            submitted_by_name=craftsman.display_name,
        )
        flash(
            "Der Auftrag wurde abgelehnt.",
            "success",
        )
    except CommissionWorkflowError as exc:
        db.session.rollback()
        flash(exc.message, "danger")

    return redirect(
        url_for(
            "external_portal.commission_detail",
            public_id=public_id,
        )
    )


@external_portal_bp.post(
    "/auftraege/<string:public_id>/abschliessen"
)
@external_craftsman_required
def commission_complete(public_id: str) -> Any:
    craftsman = current_external_craftsman()
    commission = external_commission_or_404(
        public_id
    )
    db.session.refresh(
        commission,
        with_for_update=True,
    )
    ensure_external_assignment(
        commission,
        craftsman,
    )

    completed_value = str(
        request.form.get("completed") or ""
    ).strip()

    if completed_value not in {"yes", "no"}:
        db.session.rollback()
        flash(
            "Bitte das Ergebnis des Auftrags auswählen.",
            "danger",
        )
        return redirect(
            url_for(
                "external_portal.commission_detail",
                public_id=public_id,
            )
        )

    completed = completed_value == "yes"

    try:
        reason = complete_commission(
            commission,
            completed=completed,
            reason=str(
                request.form.get("reason") or ""
            ),
            tenant_charge=False,
            enforce_material_confirmation=False,
            external_craftsman=craftsman,
        )
        db.session.commit()

        if not completed:
            send_external_status_notification(
                commission,
                event_name=(
                    "Auftrag nicht erfolgreich abgeschlossen"
                ),
                reason=reason,
                submitted_by_name=(
                    craftsman.display_name
                ),
            )

        flash(
            "Der Auftragsabschluss wurde übermittelt.",
            "success",
        )
    except CommissionWorkflowError as exc:
        db.session.rollback()
        flash(exc.message, "danger")

    return redirect(
        url_for("external_portal.commissions")
    )


def send_external_status_notification(
    commission: Commission,
    *,
    event_name: str,
    reason: str,
    submitted_by_name: str,
) -> None:
    try:
        send_commission_status_email(
            commission,
            event_name=event_name,
            reason=reason,
            submitted_by_name=submitted_by_name,
        )
    except Exception:
        current_app.logger.exception(
            "external_commission_status_email_failed "
            "commission_id=%s event=%s",
            commission.id,
            event_name,
        )


def ensure_external_assignment(
    commission: Commission,
    craftsman: ExternalCraftsman,
) -> None:
    if (
        commission.craftsman_erp_id
        != craftsman.craftsman_erp_id
    ):
        abort(404)


def external_commission_or_404(
    public_id: str,
) -> Commission:
    craftsman = current_external_craftsman()
    commission = Commission.query.filter(
        Commission.public_id == public_id,
        Commission.craftsman_erp_id
        == craftsman.craftsman_erp_id,
        Commission.deleted_at.is_(None),
        Commission.local_status
        != CommissionLocalStatus.DELETED.value,
    ).one_or_none()

    if commission is None:
        abort(404)

    return commission


def external_commission_photos(
    commission: Commission,
) -> list[CommissionPhoto]:
    photos = list(
        CommissionPhoto.query.filter(
            CommissionPhoto.commission_id
            == commission.id,
            CommissionPhoto.context == "order",
            CommissionPhoto.active.is_(True),
        ).all()
    )
    entity_conditions = []

    if commission.building_erp_id is not None:
        entity_conditions.append(
            and_(
                CommissionPhoto.external_entity_name
                == "Building",
                CommissionPhoto.external_entity_id
                == commission.building_erp_id,
            )
        )

    if commission.use_unit_id is not None:
        entity_conditions.append(
            and_(
                CommissionPhoto.external_entity_name
                == "UseUnit",
                CommissionPhoto.external_entity_id
                == commission.use_unit_id,
            )
        )

    if entity_conditions:
        photos.extend(
            CommissionPhoto.query.filter(
                CommissionPhoto.commission_id.is_(
                    None
                ),
                CommissionPhoto.source
                == MEDIA_SOURCE,
                CommissionPhoto.active.is_(True),
                or_(*entity_conditions),
            ).all()
        )

    photos.sort(
        key=lambda photo: photo.id,
        reverse=True,
    )
    return photos


def external_commission_photo_or_404(
    commission: Commission,
    photo_id: int,
) -> CommissionPhoto:
    photo = CommissionPhoto.query.filter(
        CommissionPhoto.id == photo_id,
        CommissionPhoto.active.is_(True),
    ).one_or_none()

    if photo is None:
        abort(404)

    if photo.commission_id == commission.id:
        return photo

    object_photo_matches = (
            photo.commission_id is None
            and photo.source == MEDIA_SOURCE
            and (
                    (
                            commission.building_erp_id
                            is not None
                            and photo.external_entity_name
                            == "Building"
                            and photo.external_entity_id
                            == commission.building_erp_id
                    )
                    or (
                            commission.use_unit_id
                            is not None
                            and photo.external_entity_name
                            == "UseUnit"
                            and photo.external_entity_id
                            == commission.use_unit_id
                    )
            )
    )

    if object_photo_matches:
        return photo

    abort(404)


@external_portal_bp.get(
    "/auftraege/<string:public_id>/fotos/"
    "<int:photo_id>/inhalt"
)
@external_craftsman_required
def photo_content(
    public_id: str,
    photo_id: int,
) -> Any:
    commission = external_commission_or_404(
        public_id
    )
    photo = external_commission_photo_or_404(
        commission,
        photo_id,
    )
    path = commission_photo_path(
        photo.storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype=photo.mime_type,
        as_attachment=False,
        download_name=photo.original_filename,
        conditional=True,
    )


@external_portal_bp.get(
    "/auftraege/<string:public_id>/fotos/"
    "<int:photo_id>/vorschau"
)
@external_craftsman_required
def photo_thumbnail(
    public_id: str,
    photo_id: int,
) -> Any:
    commission = external_commission_or_404(
        public_id
    )
    photo = external_commission_photo_or_404(
        commission,
        photo_id,
    )

    if not photo.thumbnail_storage_name:
        abort(404)

    path = commission_photo_path(
        photo.thumbnail_storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=False,
        conditional=True,
    )


def external_attachment_or_404(
    commission: Commission,
    attachment_id: int,
) -> CommissionAttachment:
    attachment = CommissionAttachment.query.filter(
        CommissionAttachment.id == attachment_id,
        CommissionAttachment.commission_id
        == commission.id,
        CommissionAttachment.active.is_(True),
    ).one_or_none()

    if attachment is None:
        abort(404)

    return attachment


@external_portal_bp.get(
    "/auftraege/<string:public_id>/anhaenge/"
    "<int:attachment_id>/inhalt"
)
@external_craftsman_required
def attachment_content(
    public_id: str,
    attachment_id: int,
) -> Any:
    commission = external_commission_or_404(
        public_id
    )
    attachment = external_attachment_or_404(
        commission,
        attachment_id,
    )
    path = commission_attachment_path(
        attachment.storage_name
    )

    if not path.is_file():
        abort(404)

    display_inline = (
        attachment.mime_type.startswith("image/")
        or attachment.mime_type
        == "application/pdf"
    )

    return send_file(
        path,
        mimetype=attachment.mime_type,
        as_attachment=not display_inline,
        download_name=attachment.original_filename,
        conditional=True,
    )


@external_portal_bp.get(
    "/auftraege/<string:public_id>/anhaenge/"
    "<int:attachment_id>/vorschau"
)
@external_craftsman_required
def attachment_thumbnail(
    public_id: str,
    attachment_id: int,
) -> Any:
    commission = external_commission_or_404(
        public_id
    )
    attachment = external_attachment_or_404(
        commission,
        attachment_id,
    )

    if not attachment.thumbnail_storage_name:
        abort(404)

    path = commission_attachment_path(
        attachment.thumbnail_storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=False,
        conditional=True,
    )