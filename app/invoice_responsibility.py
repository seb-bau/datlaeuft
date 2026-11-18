from uuid import uuid4

from .extensions import db
from .models import Invoice, User
from .settings import SettingKeys, settings, validate_debug_email_address
from .workflow_notifications import enqueue_notification


def assign_invoice(invoice, user_id, actor, revision):
    try:
        user_id, revision = int(user_id), int(revision)
    except (ValueError, TypeError) as exc:
        raise ValueError("Bitte einen zuständigen Benutzer auswählen.") from exc
    if revision != invoice.responsibility_revision:
        raise ValueError("Die Zuständigkeit wurde inzwischen geändert. Bitte die Seite neu laden.")
    user = db.session.get(User, user_id)
    if user is None or not user.is_active or not (user.is_admin or user.can_manage_invoices):
        raise ValueError("Der Benutzer ist nicht aktiv oder darf keine Rechnungen bearbeiten.")
    if invoice.responsible_user_id == user.id:
        return False
    try:
        recipient = validate_debug_email_address(user.email)
    except ValueError as exc:
        raise ValueError("Für den Benutzer ist keine gültige E-Mail-Adresse hinterlegt.") from exc
    if not recipient:
        raise ValueError("Für den Benutzer ist keine E-Mail-Adresse hinterlegt.")
    invoice.responsible_user = user
    invoice.responsibility_revision += 1
    enqueue_notification(f"invoice-assignment:{invoice.id}:{uuid4()}", "invoice_assignment", payload={
        "invoice_id": invoice.id, "user_id": user.id, "revision": invoice.responsibility_revision,
        "actor_name": actor.display_name,
    })
    return True


def assignment_is_current(item):
    invoice = db.session.get(Invoice, item.payload.get("invoice_id"), populate_existing=True)
    user = db.session.get(User, item.payload.get("user_id"), populate_existing=True)
    return bool(
        invoice is not None and user is not None and user.is_active
        and (user.is_admin or user.can_manage_invoices)
        and invoice.responsible_user_id == user.id
        and invoice.responsibility_revision == item.payload.get("revision")
    )


def assignment_content(item):
    invoice = db.session.get(Invoice, item.payload["invoice_id"])
    user = db.session.get(User, item.payload["user_id"])
    reference = invoice.invoice_number or f"Entwurf {invoice.id}"
    base = settings.get(SettingKeys.PUBLIC_BASE_URL).rstrip("/")
    return validate_debug_email_address(user.email), {
        "subject": f"Rechnung {reference}: Sie sind zuständig",
        "body": (
            f"Guten Tag {user.display_name},\n\n"
            f"{item.payload['actor_name']} hat Ihnen die Zuständigkeit für die Rechnung {reference} übertragen.\n"
            f"Empfänger: {invoice.recipient_name or 'Noch nicht angegeben'}\n"
            f"Buchwerk: {invoice.company_code_code}\n\n"
            f"Rechnung öffnen: {base}/admin/invoices/{invoice.id}"
        ),
    }
