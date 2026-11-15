from email.utils import parseaddr
from hashlib import sha256

from .extensions import db
from .models import Commission, CommissionTicketLink, TicketMirrorState, WowiLicenseAgreement
from .settings import validate_debug_email_address
from .tenant_notifications import tenant_email_addresses
from .ticket_service import fetch_ticket_row, ticket_license_agreement_local_id, value
from .workflow_notifications import enqueue_notification
from .wowiport import WowiportService


def ticket_reply_recipients(ticket):
    service = WowiportService()
    row = fetch_ticket_row(ticket.erp_id, service)
    candidates = [value(row, name) for name in ("ReplyTo", "SenderEmail", "EmailFrom", "Email")]
    sender = value(row, "Sender")
    if isinstance(sender, dict):
        candidates.extend(value(sender, name) for name in ("Email", "Address"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        address = parseaddr(candidate)[1]
        try:
            address = validate_debug_email_address(address)
        except ValueError:
            continue
        if address:
            return [address]
    agreement_id = ticket_license_agreement_local_id(ticket)
    agreement = db.session.get(WowiLicenseAgreement, agreement_id) if agreement_id is not None else None
    if agreement is None or agreement.is_vacancy:
        return []
    commission = Commission(license_agreement_id=agreement.erp_id)
    return tenant_email_addresses(commission, service)


def enqueue_ticket_reply(ticket, recipient, response_text):
    recipient = validate_debug_email_address(recipient)
    response_text = str(response_text or "").strip()
    if not recipient:
        raise ValueError("Bitte eine Empfängeradresse für die Antwort angeben.")
    if not response_text or len(response_text) > 20000:
        raise ValueError("Bitte eine Antwort mit höchstens 20.000 Zeichen eingeben.")
    digest = sha256((recipient.casefold() + "\n" + response_text).encode()).hexdigest()
    key = f"ticket-reply:{ticket.id}:{digest}"
    if ticket.deleted_at is not None or ticket.state == TicketMirrorState.CONVERTED.value or ticket.commission_id is not None or CommissionTicketLink.query.filter_by(ticket_erp_id=ticket.erp_id).first() is not None:
        raise ValueError("Für dieses Ticket wurde bereits ein Auftrag erstellt oder das Ticket ist nicht mehr verfügbar.")
    item = enqueue_notification(key, "ticket_reply", ticket=ticket, payload={
        "recipient": recipient, "response_text": response_text,
    })
    ticket.state = TicketMirrorState.IGNORED.value
    return item
