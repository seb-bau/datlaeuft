from __future__ import annotations

import secrets as secret_factory

import click
from flask import Flask

from .catalog import sync_catalogs_from_wowiport
from .entra import import_entra_users
from .erp_sync import process_due_work, run_worker
from .extensions import db
from .location_sync import sync_wowi_buildings
from .object_directory import sync_object_directory
from .models import (
    ErpOutboxOperation,
    Commission,
    CommissionLocalStatus,
    User,
    WowiCommissionType,
    utc_now,
    WowiCompanyCode,
)
from .settings import SettingKeys, settings
from .push_service import (
    enqueue_push_ping,
    process_push_batch,
    push_batch_state_counts,
)
from .wowiport import (
    sync_commission_from_erp,
    sync_commission_types_from_wowiport,
    sync_commissions_from_wowiport_regular, sync_company_codes_from_wowiport,
)
from .media_sync import (
    sync_object_photos_from_wowiport,
)
from .offer_service import (
    reconcile_offer_workflows,
)
from .safety_inspection_service import (
    generate_safety_inspection_jobs,
    reconcile_safety_inspection_defects,
)
from .safety_inspection_archive import (
    process_pending_safety_inspection_archives,
)
from .safety_inspection_service import (
    generate_safety_inspection_jobs,
    reconcile_safety_inspection_assignments,
    reconcile_safety_inspection_defects,
)
from .process_info_service import (
    bootstrap_process_infos,
)

from datetime import datetime
from .ticket_service import sync_all_ticket_mirrors


def register_cli(app: Flask) -> None:
    @app.cli.command("datlaeuft-sync-company-codes")
    def sync_company_codes() -> None:
        result = sync_company_codes_from_wowiport()
        db.session.commit()

        click.echo(
            f"Gefunden={result['seen']} "
            f"Neu={result['created']} "
            f"Aktualisiert={result['updated']}"
        )

        for item in WowiCompanyCode.query.order_by(
            WowiCompanyCode.code.asc()
        ).all():
            state = "aktiv" if item.active else "inaktiv"
            click.echo(
                f"{item.erp_id}\t"
                f"{item.code}\t"
                f"{item.name}\t"
                f"{state}"
            )

    @app.cli.command("datlaeuft-sync-tickets")
    def sync_tickets() -> None:
        result = sync_all_ticket_mirrors()
        db.session.commit()

        click.echo(
            f"Gefunden={result['seen']} "
            f"Neu={result['created']} "
            f"Aktualisiert={result['updated']}"
        )

    @app.cli.command("datlaeuft-retry-erp-operation")
    @click.argument("operation_id", type=int)
    def retry_erp_operation(operation_id: int) -> None:
        operation = db.session.get(
            ErpOutboxOperation,
            operation_id,
        )

        if operation is None:
            raise click.ClickException(
                f"ERP-Operation {operation_id} wurde nicht gefunden"
            )

        if operation.state == "succeeded":
            raise click.ClickException(
                f"ERP-Operation {operation_id} wurde bereits abgeschlossen"
            )

        operation.state = "pending"
        operation.attempt_count = 0
        operation.available_at = utc_now()
        operation.locked_at = None
        operation.last_error = None
        operation.completed_at = None

        db.session.commit()

        click.echo(
            f"ERP-Operation {operation_id} wurde erneut freigegeben"
        )

    @app.cli.command("datlaeuft-seed-settings")
    def seed_settings() -> None:
        settings.ensure_defaults()

        if settings.get(SettingKeys.APP_SECRET_KEY) == "change-me":
            settings.set(
                SettingKeys.APP_SECRET_KEY,
                secret_factory.token_urlsafe(48),
            )

        if settings.get(SettingKeys.JWT_SECRET_KEY) == "change-me":
            settings.set(
                SettingKeys.JWT_SECRET_KEY,
                secret_factory.token_urlsafe(48),
            )

        db.session.commit()
        click.echo("Einstellungen wurden angelegt.")

    @app.cli.command("datlaeuft-create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option("--password")
    def create_admin(
        email: str,
        name: str,
        password: str,
    ) -> None:
        user = User.query.filter_by(
            email=email.lower()
        ).one_or_none()

        if user is None:
            user = User(
                email=email.lower(),
                display_name=name,
            )
            db.session.add(user)

        user.display_name = name
        user.is_admin = True
        user.is_foreman = True
        user.is_active_flag = True
        user.set_password(password)

        db.session.commit()
        click.echo("Administrator wurde gespeichert.")

    @app.cli.command("datlaeuft-import-entra-users")
    def import_entra() -> None:
        result = import_entra_users()

        click.echo(
            f"Neu={result.created} "
            f"Aktualisiert={result.updated} "
            f"Übersprungen={result.skipped}"
        )

    @app.cli.command("datlaeuft-sync-commission-types")
    def sync_commission_types() -> None:
        result = sync_commission_types_from_wowiport()
        db.session.commit()

        click.echo(
            f"Gefunden={result['seen']} "
            f"Neu={result['created']} "
            f"Aktualisiert={result['updated']}"
        )

        for item in WowiCommissionType.query.order_by(
            WowiCommissionType.code.asc(),
            WowiCommissionType.erp_id.asc(),
        ).all():
            state = "aktiv" if item.active else "inaktiv"
            click.echo(
                f"{item.erp_id}\t"
                f"{item.code or '-'}\t"
                f"{state}"
            )

    @app.cli.command("datlaeuft-set-commission-types")
    @click.argument("erp_ids", nargs=-1, type=int)
    def set_commission_types(
        erp_ids: tuple[int, ...],
    ) -> None:
        selected_ids = sorted(set(erp_ids))

        settings.set(
            SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS,
            selected_ids,
        )
        db.session.commit()

        if selected_ids:
            selected_text = ", ".join(
                str(erp_id)
                for erp_id in selected_ids
            )
        else:
            selected_text = "keine"

        click.echo(
            f"Ausgewählte Auftragsarten: {selected_text}"
        )

    @app.cli.command("datlaeuft-sync-commission")
    @click.argument("erp_id", type=int)
    def sync_commission(erp_id: int) -> None:
        commission = sync_commission_from_erp(erp_id)
        db.session.commit()

        if commission is None:
            click.echo("Auftrag wurde ignoriert.")
        else:
            click.echo(
                f"Auftrag wurde synchronisiert: "
                f"lokale ID={commission.id}"
            )

    @app.cli.command("datlaeuft-sync-commission-contexts")
    def sync_commission_contexts() -> None:
        commissions = Commission.query.filter(
            Commission.local_status
            != CommissionLocalStatus.DELETED.value,
            Commission.erp_id.isnot(None),
        ).all()

        synced_count = 0

        for commission in commissions:
            sync_commission_from_erp(commission.erp_id)
            synced_count += 1

        db.session.commit()
        click.echo(
            f"Auftragskontexte synchronisiert: {synced_count}"
        )

    @app.cli.command(
        "datlaeuft-sync-service-catalog"
    )
    def sync_service_catalog() -> None:
        result = sync_catalogs_from_wowiport()
        db.session.commit()

        click.echo(
            f"Gewerke_neu="
            f"{result.craft_activities_created} "
            f"Gewerke_aktualisiert="
            f"{result.craft_activities_updated} "
            f"Leistungen_neu="
            f"{result.service_positions_created} "
            f"Leistungen_aktualisiert="
            f"{result.service_positions_updated} "
            f"Leistungen_deaktivierte_Gewerke="
            f"{result.service_positions_skipped_disabled_craft_activity} "
            f"Leistungen_ohne_Gewerk="
            f"{result.service_positions_without_craft_activity} "
            f"Leistungen_deaktiviert="
            f"{result.service_positions_deactivated}"
        )

    @app.cli.command(
        "datlaeuft-sync-commissions"
    )
    def sync_commissions() -> None:
        result = (
            sync_commissions_from_wowiport_regular()
        )
        offer_result = (
            reconcile_offer_workflows()
        )
        db.session.commit()

        click.echo(
            f"Modus=vollständig "
            f"Handwerker={result['craftsmen']} "
            f"Status={result['statuses']} "
            f"Gesehen={result['seen']} "
            f"Synchronisiert={result['synced']} "
            f"Unverändert={result['unchanged']} "
            f"Falscher_Status="
            f"{result['ignored_status']} "
            f"Falsche_Auftragsart="
            f"{result['ignored_type']} "
            f"Falsches_Geschäftsjahr="
            f"{result['ignored_business_year']} "
            f"Unbekannter_Handwerker="
            f"{result['ignored_unknown_craftsman']} "
            f"Ausgeblendet="
            f"{result['deleted_marked']} "
            f"Fehler={result['errors']} "
            f"Konfiguration_fehlt="
            f"{result['configuration_missing']} "
            f"Abgleich_übersprungen="
            f"{result['reconciliation_skipped']}"
        )
        click.echo(
            f"Angebote_geprüft="
            f"{offer_result['seen']} "
            f"Angebote_geändert="
            f"{offer_result['changed']} "
            f"Angebote_unverändert="
            f"{offer_result['unchanged']} "
            f"Angebote_abgelaufen="
            f"{offer_result['expired']} "
            f"Rechnungen_erstellt="
            f"{offer_result['invoices_created']} "
            f"Angebotsfehler="
            f"{offer_result['errors']}"
        )

    @app.cli.command(
        "datlaeuft-reconcile-offers"
    )
    @click.option(
        "--offer-id",
        type=int,
        default=None,
    )
    def reconcile_offers(
        offer_id: int | None,
    ) -> None:
        result = (
            reconcile_offer_workflows(
                offer_id=offer_id
            )
        )
        db.session.commit()

        click.echo(
            f"Geprüft={result['seen']} "
            f"Geändert={result['changed']} "
            f"Unverändert={result['unchanged']} "
            f"Abgelaufen={result['expired']} "
            f"Rechnungen_erstellt="
            f"{result['invoices_created']} "
            f"Fehler={result['errors']}"
        )

    @app.cli.command(
        "datlaeuft-sync-object-photos"
    )
    def sync_object_photos() -> None:
        result = (
            sync_object_photos_from_wowiport()
        )

        click.echo(
            f"Gefunden={result['seen']} "
            f"Neu={result['created']} "
            f"Aktualisiert={result['updated']} "
            f"Unverändert={result['unchanged']} "
            f"Heruntergeladen="
            f"{result['downloaded']} "
            f"Gelöscht={result['deleted']} "
            f"Altbestand_gelöscht="
            f"{result['legacy_deleted']} "
            f"Dateien_gelöscht="
            f"{result['deleted_files']} "
            f"Fehler={result['errors']}"
        )

    @app.cli.command("datlaeuft-sync-buildings")
    def sync_buildings() -> None:
        result = sync_wowi_buildings()

        click.echo(
            f"Gesehen={result['seen']} "
            f"Neu={result['created']} "
            f"Aktualisiert={result['updated']} "
            f"Koordinaten_beibehalten="
            f"{result['kept_existing_coordinates']} "
            f"Geokodiert={result['geocoded']} "
            f"Ohne_Koordinaten={result['without_coordinates']} "
            f"Übersprungen={result['skipped']}"
        )

    @app.cli.command(
        "datlaeuft-sync-object-directory"
    )
    def sync_commission_object_directory() -> None:
        result = sync_object_directory()
        db.session.commit()

        click.echo(
            f"Abteilungen="
            f"{result['departments']} "
            f"Wirtschaftseinheiten="
            f"{result['economic_units']} "
            f"Zuständigkeiten="
            f"{result['jurisdictions']} "
            f"Gebäude="
            f"{result['buildings']} "
            f"Nutzungseinheiten="
            f"{result['use_units']} "
            f"Mietverträge="
            f"{result['license_agreements']} "
            f"Vertragspartner="
            f"{result['contractors']}"
        )

    @app.cli.command(
        "datlaeuft-run-safety-inspections"
    )
    @click.option(
        "--run-date",
        type=click.DateTime(
            formats=["%Y-%m-%d"]
        ),
        default=None,
    )
    @click.option(
        "--group-id",
        type=int,
        default=None,
    )
    @click.option(
        "--archive-limit",
        type=int,
        default=50,
        show_default=True,
    )
    def run_safety_inspections(
        run_date: datetime | None,
        group_id: int | None,
        archive_limit: int,
    ) -> None:
        effective_date = (
            run_date.date()
            if run_date is not None
            else None
        )

        try:
            generation_result = (
                generate_safety_inspection_jobs(
                    run_date=effective_date,
                    group_id=group_id,
                )
            )
            assignment_result = (
                reconcile_safety_inspection_assignments()
            )
            defect_result = (
                reconcile_safety_inspection_defects()
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        archive_result = (
            process_pending_safety_inspection_archives(
                limit=max(
                    archive_limit,
                    0,
                )
            )
        )

        click.echo(
            "Prüfgruppen="
            f"{generation_result['groups']} "
            "Neu="
            f"{generation_result['created']} "
            "Vorhanden="
            f"{generation_result['existing']} "
            "Unzugeordnet="
            f"{generation_result['unassigned']} "
            "Abgelaufen="
            f"{generation_result['missed']} "
            "Fehler="
            f"{generation_result['errors']}"
        )

        click.echo(
            "Zuordnungen_geprüft="
            f"{assignment_result['seen']} "
            "Neu_zugeordnet="
            f"{assignment_result['assigned']} "
            "Weiterhin_unzugeordnet="
            f"{assignment_result['unresolved']}"
        )

        click.echo(
            "Mängel_geprüft="
            f"{defect_result['seen']} "
            "Mängel_geändert="
            f"{defect_result['changed']} "
            "Mängel_unverändert="
            f"{defect_result['unchanged']}"
        )

        click.echo(
            "Protokolle_geprüft="
            f"{archive_result['seen']} "
            "Archiviert="
            f"{archive_result['archived']} "
            "Bereits_vorhanden="
            f"{archive_result['existing']} "
            "Archivfehler="
            f"{archive_result['failed']} "
            "Fotos_geprüft="
            f"{archive_result['photo_seen']} "
            "Fotos_archiviert="
            f"{archive_result['photo_archived']} "
            "Fotos_vorhanden="
            f"{archive_result['photo_existing']} "
            "Fotofehler="
            f"{archive_result['photo_failed']} "
            "Archivierung_deaktiviert="
            f"{archive_result['disabled']}"
        )

    @app.cli.command("datlaeuft-push-ping")
    @click.option("--email", required=True)
    @click.option(
        "--message",
        default="Push-Verbindung erfolgreich getestet.",
        show_default=True,
    )
    def push_ping(
        email: str,
        message: str,
    ) -> None:
        user = User.query.filter_by(
            email=email.strip().lower(),
            is_active_flag=True,
        ).one_or_none()

        if user is None:
            raise click.ClickException(
                "Der aktive Benutzer wurde nicht gefunden."
            )

        batch = enqueue_push_ping(
            user=user,
            message=message,
        )
        db.session.commit()

        if batch.queued_count == 0:
            click.echo(
                "Für den Benutzer ist kein aktives iOS-Gerät registriert."
            )
            return

        processed_count = process_push_batch(
            limit=batch.queued_count,
            batch_id=batch.batch_id,
        )
        state_counts = push_batch_state_counts(batch.batch_id)

        click.echo(
            f"Eingeplant={batch.queued_count} "
            f"Verarbeitet={processed_count} "
            f"Zugestellt={state_counts.get('succeeded', 0)} "
            f"Fehlgeschlagen={state_counts.get('failed', 0)} "
            f"Verworfen={state_counts.get('discarded', 0)}"
        )

    @app.cli.command("datlaeuft-process-erp-work")
    @click.option("--outbox-limit", type=int, default=20)
    @click.option("--webhook-limit", type=int, default=50)
    @click.option("--push-limit", type=int, default=50)
    def process_erp_work(
        outbox_limit: int,
        webhook_limit: int,
        push_limit: int,
    ) -> None:
        result = process_due_work(
            outbox_limit=outbox_limit,
            webhook_limit=webhook_limit,
            push_limit=push_limit,
        )

        click.echo(
            f"Webhooks={result['webhooks']} "
            f"ERP-Aufgaben={result['outbox']} "
            f"Vorgangsinfos={result['process_infos']} "
            f"Auftragnehmer-Mails="
            f"{result['external_assignment_emails']} "
            f"Push-Zustellungen={result['push']}"
        )

    @app.cli.command(
        "datlaeuft-bootstrap-process-infos"
    )
    def bootstrap_process_info_history() -> None:
        (
            process_count,
            info_count,
        ) = bootstrap_process_infos()

        click.echo(
            f"Vorgänge={process_count} "
            f"Informationen={info_count}"
        )

    @app.cli.command("datlaeuft-worker")
    @click.option("--poll-seconds", type=int, default=10)
    def worker(
        poll_seconds: int,
    ) -> None:
        run_worker(
            poll_seconds=max(poll_seconds, 1),
        )