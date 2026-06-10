from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

import jwt as pyjwt
import msal
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from jwt import PyJWKClient

from .audit import audit
from .extensions import db, login_manager
from .models import User, utc_now
from .settings import SettingKeys, settings

F = TypeVar("F", bound=Callable[..., Any])

auth_bp = Blueprint("auth", __name__)


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    if not user_id.isdigit():
        return None
    return db.session.get(User, int(user_id))


def admin_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.full_path))
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def foreman_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.full_path))
        if not (current_user.is_admin or current_user.is_foreman):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        if not settings.get(SettingKeys.PASSWORD_LOGIN_ENABLED):
            flash("Kennwort-Login ist deaktiviert.", "warning")
            return redirect(url_for("auth.login"))
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).one_or_none()
        if user and user.check_password(password) and user.is_active:
            user.last_login_at = utc_now()
            login_user(user)
            audit("auth.password_login", "user", user.id)
            db.session.commit()
            return redirect(request.args.get("next") or url_for("admin.dashboard"))
        flash("Anmeldung fehlgeschlagen.", "danger")
    return render_template("auth/login.html", password_enabled=settings.get(SettingKeys.PASSWORD_LOGIN_ENABLED), microsoft_enabled=settings.get(SettingKeys.MICROSOFT_LOGIN_ENABLED))


@auth_bp.route("/logout")
@login_required
def logout() -> Any:
    audit("auth.logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/auth/microsoft")
def microsoft_login() -> Any:
    if not settings.get(SettingKeys.MICROSOFT_LOGIN_ENABLED):
        abort(404)
    flow = _build_msal_app().initiate_auth_code_flow(scopes=["User.Read"], redirect_uri=url_for("auth.microsoft_callback", _external=True))
    session["msal_flow"] = flow
    return redirect(flow["auth_uri"])


@auth_bp.route("/auth/microsoft/callback")
def microsoft_callback() -> Any:
    flow = session.get("msal_flow", {})
    result = _build_msal_app().acquire_token_by_auth_code_flow(flow, request.args)
    if "error" in result:
        current_app.logger.warning("microsoft_login_failed %s", result.get("error_description"))
        flash("Microsoft-Anmeldung fehlgeschlagen.", "danger")
        return redirect(url_for("auth.login"))
    claims = result.get("id_token_claims", {})
    user = resolve_microsoft_user(claims, settings.get(SettingKeys.MICROSOFT_AUTO_CREATE_USERS))
    if user is None:
        flash("Der Microsoft-Benutzer ist nicht freigeschaltet.", "danger")
        return redirect(url_for("auth.login"))
    user.last_login_at = utc_now()
    login_user(user)
    audit("auth.microsoft_login", "user", user.id, {"oid": user.entra_oid})
    db.session.commit()
    return redirect(url_for("admin.dashboard"))


def _build_msal_app() -> msal.ConfidentialClientApplication:
    tenant_id = settings.get(SettingKeys.ENTRA_TENANT_ID)
    client_id = settings.get(SettingKeys.ENTRA_CLIENT_ID)
    client_secret = settings.get(SettingKeys.ENTRA_CLIENT_SECRET)
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    return msal.ConfidentialClientApplication(client_id=client_id, client_credential=client_secret, authority=authority)


def resolve_microsoft_user(claims: dict[str, Any], create: bool) -> User | None:
    oid = claims.get("oid")
    email = (claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "").strip().lower()
    name = claims.get("name") or email
    if not email:
        return None
    user = User.query.filter((User.entra_oid == oid) | (User.email == email)).one_or_none()
    if user is None and create:
        user = User(email=email, display_name=name, entra_oid=oid, is_active_flag=True)
        db.session.add(user)
    if user is None:
        return None
    user.entra_oid = oid or user.entra_oid
    user.email = email
    user.display_name = name or user.display_name
    return user


def validate_microsoft_id_token(id_token: str) -> dict[str, Any]:
    tenant_id = settings.get(SettingKeys.ENTRA_TENANT_ID)
    client_ids = [x.strip() for x in settings.get(SettingKeys.MICROSOFT_ALLOWED_CLIENT_IDS).split(",") if x.strip()]

    default_client_id = settings.get(SettingKeys.ENTRA_CLIENT_ID).strip()
    if default_client_id and default_client_id not in client_ids:
        client_ids.append(default_client_id)

    mobile_client_id = settings.get(SettingKeys.MOBILE_MICROSOFT_CLIENT_ID).strip()
    if mobile_client_id and mobile_client_id not in client_ids:
        client_ids.append(mobile_client_id)
    jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(id_token)
    return pyjwt.decode(id_token, signing_key.key, algorithms=["RS256"], audience=client_ids, issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0")