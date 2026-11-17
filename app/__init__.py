from __future__ import annotations

from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from . import models
from .admin import admin_bp
from .api import api_bp
from .mobile_sync import mobile_sync_bp
from .auth import auth_bp
from .process_info_api import process_info_api_bp
from .cli import register_cli
from .extensions import cors, csrf, db, jwt, login_manager, migrate
from .logging_service import configure_logging
from .settings import SettingKeys, settings
from .webhooks import webhooks_bp
from .health import health_bp
from .appointments import appointments_bp
from .safety_inspection_admin import (
    safety_inspection_admin_bp,
)
from .safety_inspection_jobs_admin import (
    safety_inspection_jobs_admin_bp,
)
from .safety_inspection_api import (
    safety_inspection_api_bp,
)
from .safety_inspection_protocol_admin import (
    safety_inspection_protocol_admin_bp,
)
from .external_portal import external_portal_bp
from .process_info_web import process_info_web_bp
from .dms_admin import dms_admin_bp
from .blacklist import init_blacklist
from .blacklist_admin import blacklist_bp
from .document_articles import document_articles_bp
from .invoice_delivery import invoice_delivery_bp

# Worker starten: flask datlaeuft-worker --poll-seconds 10
# fällige Aufgaben: flask datlaeuft-process-erp-work


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI="sqlite:///datlaeuft-dev.sqlite",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="bootstrap",
        JWT_SECRET_KEY="bootstrap",
        JWT_ACCESS_TOKEN_EXPIRES=False,
        JSON_SORT_KEYS=False,
    )
    app.config.from_pyfile("config.py", silent=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    jwt.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Bitte anmelden."

    init_blacklist(app)
    app.register_blueprint(blacklist_bp)

    with app.app_context():
        load_runtime_config(app)
        try:
            configure_logging(app)
        except Exception as exc:
            app.logger.warning("logging_configuration_fallback %s", exc)

    cors.init_app(app, resources={r"/api/*": {"origins": cors_origins()}})

    app.register_blueprint(auth_bp)
    app.register_blueprint(external_portal_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(document_articles_bp)
    app.register_blueprint(invoice_delivery_bp)
    app.register_blueprint(dms_admin_bp)
    app.register_blueprint(
        safety_inspection_admin_bp
    )
    app.register_blueprint(
        safety_inspection_jobs_admin_bp
    )
    app.register_blueprint(api_bp)
    app.register_blueprint(mobile_sync_bp)
    app.register_blueprint(process_info_api_bp)
    app.register_blueprint(process_info_web_bp)
    app.register_blueprint(
        safety_inspection_api_bp
    )
    app.register_blueprint(health_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(appointments_bp)
    app.register_blueprint(
        safety_inspection_protocol_admin_bp
    )
    csrf.exempt(api_bp)
    csrf.exempt(mobile_sync_bp)
    csrf.exempt(process_info_api_bp)
    csrf.exempt(
        safety_inspection_api_bp
    )
    csrf.exempt(webhooks_bp)
    register_cli(app)
    return app


def load_runtime_config(app: Flask) -> None:
    try:
        app.config["SECRET_KEY"] = settings.get(SettingKeys.APP_SECRET_KEY)
        app.config["JWT_SECRET_KEY"] = settings.get(SettingKeys.JWT_SECRET_KEY)
        app.config["JWT_ACCESS_TOKEN_EXPIRES"] = False if settings.get(SettingKeys.MOBILE_TOKEN_NEVER_EXPIRES) else 43_200
    except SQLAlchemyError:
        pass
    except Exception:
        pass


def cors_origins() -> list[str] | str:
    try:
        raw = settings.get(SettingKeys.CORS_ORIGINS)
    except Exception:
        return "*"
    if raw.strip() == "*":
        return "*"
    return [item.strip() for item in raw.split(",") if item.strip()]
