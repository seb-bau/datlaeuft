from __future__ import annotations

import secrets as secret_factory

import click
from flask import Flask

from .entra import import_entra_users
from .extensions import db
from .models import User
from .settings import SettingKeys, settings
from .wowiport import sync_commission_from_erp
from .wowiport import sync_commissions_from_wowiport_regular
from .catalog import sync_catalogs_from_wowiport
from .location_sync import sync_wowi_buildings


def register_cli(app: Flask) -> None:
    @app.cli.command("datlaeuft-seed-settings")
    def seed_settings() -> None:
        settings.ensure_defaults()
        if settings.get(SettingKeys.APP_SECRET_KEY) == "change-me":
            settings.set(SettingKeys.APP_SECRET_KEY, secret_factory.token_urlsafe(48))
        if settings.get(SettingKeys.JWT_SECRET_KEY) == "change-me":
            settings.set(SettingKeys.JWT_SECRET_KEY, secret_factory.token_urlsafe(48))
        db.session.commit()
        click.echo("settings seeded")

    @app.cli.command("datlaeuft-create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option("--password")
    def create_admin(email: str, name: str, password: str) -> None:
        user = db.session.query(User).filter_by(email=email.lower()).one_or_none()
        if user is None:
            user = User(email=email.lower(), display_name=name)
            db.session.add(user)
        user.display_name = name
        user.is_admin = True
        user.is_foreman = True
        user.is_active_flag = True
        user.set_password(password)
        db.session.commit()
        click.echo("admin saved")

    @app.cli.command("datlaeuft-import-entra-users")
    def import_entra() -> None:
        result = import_entra_users()
        click.echo(f"created={result.created} updated={result.updated} skipped={result.skipped}")

    @app.cli.command("datlaeuft-sync-commission")
    @click.argument("erp_id", type=int)
    def sync_commission(erp_id: int) -> None:
        commission = sync_commission_from_erp(erp_id)
        db.session.commit()
        click.echo(f"commission={commission.id if commission else 'ignored'}")

    @app.cli.command("datlaeuft-sync-commission-contexts")
    def sync_commission_contexts() -> None:
        from .models import Commission
        from .wowiport import sync_commission_from_erp

        count = 0

        for commission in db.session.query(Commission).filter(Commission.local_status != "deleted").all():
            sync_commission_from_erp(commission.erp_id)
            count += 1

        db.session.commit()
        click.echo(f"synced={count}")

    @app.cli.command("datlaeuft-sync-service-catalog")
    def sync_service_catalog() -> None:
        result = sync_catalogs_from_wowiport()
        db.session.commit()
        click.echo(
            f"craft_activities_created={result.craft_activities_created} "
            f"craft_activities_updated={result.craft_activities_updated} "
            f"service_positions_created={result.service_positions_created} "
            f"service_positions_updated={result.service_positions_updated} "
            f"service_positions_skipped_unassigned_craft_activity={result.service_positions_skipped_unassigned_craft_activity} "
            f"service_positions_without_craft_activity={result.service_positions_without_craft_activity} "
            f"service_positions_deactivated={result.service_positions_deactivated}"
        )

    @app.cli.command("datlaeuft-sync-commissions")
    def sync_commissions() -> None:
        result = sync_commissions_from_wowiport_regular()
        click.echo(
            f"craftsmen={result['craftsmen']} "
            f"seen={result['seen']} "
            f"synced={result['synced']} "
            f"ignored_status={result['ignored_status']} "
            f"ignored_business_year={result['ignored_business_year']} "
            f"ignored_unknown_craftsman={result['ignored_unknown_craftsman']} "
            f"deleted_marked={result['deleted_marked']} "
            f"errors={result['errors']}"
        )

    @app.cli.command("datlaeuft-sync-buildings")
    def sync_buildings() -> None:
        result = sync_wowi_buildings()
        click.echo(
            f"seen={result['seen']} "
            f"created={result['created']} "
            f"updated={result['updated']} "
            f"kept_existing_coordinates={result['kept_existing_coordinates']} "
            f"geocoded={result['geocoded']} "
            f"without_coordinates={result['without_coordinates']} "
            f"skipped={result['skipped']}"
        )
