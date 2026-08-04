from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
)

from .appointment_service import (
    appointment_timezone,
    send_appointment_response_email,
)
from .audit import audit
from .extensions import db
from .models import (
    AppointmentRequestStatus,
    CommissionAppointmentProposal,
    CommissionAppointmentRequest,
    utc_now,
)


appointments_bp = Blueprint(
    "appointments",
    __name__,
    url_prefix="/termine",
)


def public_token_hash(
    token: str,
) -> str:
    return sha256(
        token.encode("utf-8")
    ).hexdigest()


def appointment_request_expired(
    item: CommissionAppointmentRequest,
) -> bool:
    expires_at = item.expires_at
    now = utc_now()

    if expires_at.tzinfo is None:
        now = now.replace(
            tzinfo=None
        )

    return expires_at < now


def notify_assigned_user(
    appointment_request: CommissionAppointmentRequest,
    accepted_proposal: CommissionAppointmentProposal | None,
) -> None:
    try:
        send_appointment_response_email(
            appointment_request,
            accepted_proposal,
        )
    except (
        RuntimeError,
        ValueError,
    ):
        current_app.logger.exception(
            "appointment_response_notification_failed "
            "appointment_request_id=%s commission_id=%s",
            appointment_request.id,
            appointment_request.commission_id,
        )


@appointments_bp.route(
    "/<string:token>",
    methods=["GET", "POST"],
)
def appointment_response(
    token: str,
) -> Any:
    appointment_request = (
        CommissionAppointmentRequest.query
        .filter_by(
            token_hash=public_token_hash(
                token
            )
        )
        .one_or_none()
    )

    if appointment_request is None:
        return render_template(
            "appointments/result.html",
            state="invalid",
            appointment_request=None,
            accepted_proposal=None,
        ), 404

    if (
        appointment_request.status
        == AppointmentRequestStatus.PENDING.value
        and appointment_request_expired(
            appointment_request
        )
    ):
        appointment_request.status = (
            AppointmentRequestStatus.EXPIRED.value
        )

        db.session.commit()

    if request.method == "GET":
        if (
            appointment_request.status
            != AppointmentRequestStatus.PENDING.value
        ):
            accepted_proposal = next(
                (
                    proposal
                    for proposal
                    in appointment_request.proposals
                    if proposal.accepted
                ),
                None,
            )

            return render_template(
                "appointments/result.html",
                state=appointment_request.status,
                appointment_request=appointment_request,
                accepted_proposal=accepted_proposal,
            )

        return render_template(
            "appointments/respond.html",
            appointment_request=appointment_request,
            error=None,
        )

    if (
        appointment_request.status
        != AppointmentRequestStatus.PENDING.value
    ):
        accepted_proposal = next(
            (
                proposal
                for proposal
                in appointment_request.proposals
                if proposal.accepted
            ),
            None,
        )

        return render_template(
            "appointments/result.html",
            state=appointment_request.status,
            appointment_request=appointment_request,
            accepted_proposal=accepted_proposal,
        )

    action = request.form.get(
        "action",
        "",
    ).strip()

    tenant_comment = (
        request.form.get(
            "tenant_comment",
            "",
        ).strip()[:2000]
        or None
    )

    if action == "accept":
        try:
            proposal_id = int(
                request.form.get(
                    "proposal_id",
                    "",
                )
            )
        except ValueError:
            return render_template(
                "appointments/respond.html",
                appointment_request=appointment_request,
                error="Bitte wählen Sie einen Terminvorschlag aus.",
            ), 400

        proposal = (
            CommissionAppointmentProposal.query
            .filter_by(
                id=proposal_id,
                appointment_request_id=(
                    appointment_request.id
                ),
            )
            .one_or_none()
        )

        if proposal is None:
            return render_template(
                "appointments/respond.html",
                appointment_request=appointment_request,
                error="Der ausgewählte Terminvorschlag ist ungültig.",
            ), 400

        for item in appointment_request.proposals:
            item.accepted = (
                item.id == proposal.id
            )

        timezone: ZoneInfo = (
            appointment_timezone()
        )

        appointment_from = datetime.combine(
            proposal.proposed_date,
            proposal.start_time,
        ).replace(
            tzinfo=timezone
        )

        appointment_to = datetime.combine(
            proposal.proposed_date,
            proposal.end_time,
        ).replace(
            tzinfo=timezone
        )

        commission = (
            appointment_request.commission
        )

        commission.accepted_appointment_from = (
            appointment_from
        )
        commission.accepted_appointment_to = (
            appointment_to
        )
        commission.accepted_appointment_comment = (
            tenant_comment
        )

        appointment_request.status = (
            AppointmentRequestStatus.ACCEPTED.value
        )
        appointment_request.tenant_comment = (
            tenant_comment
        )
        appointment_request.responded_at = (
            utc_now()
        )

        (
            CommissionAppointmentRequest.query
            .filter(
                CommissionAppointmentRequest.commission_id
                == commission.id,
                CommissionAppointmentRequest.id
                != appointment_request.id,
                CommissionAppointmentRequest.status
                == AppointmentRequestStatus.PENDING.value,
            )
            .update(
                {
                    CommissionAppointmentRequest.status:
                        AppointmentRequestStatus.SUPERSEDED.value
                },
                synchronize_session=False,
            )
        )

        audit(
            "appointment.accepted",
            "commission",
            commission.id,
            {
                "appointment_request_id": appointment_request.id,
                "proposal_id": proposal.id,
                "appointment_from": appointment_from.isoformat(),
                "appointment_to": appointment_to.isoformat(),
                "tenant_comment": tenant_comment,
            },
        )

        db.session.commit()

        notify_assigned_user(
            appointment_request,
            proposal,
        )

        return render_template(
            "appointments/result.html",
            state=AppointmentRequestStatus.ACCEPTED.value,
            appointment_request=appointment_request,
            accepted_proposal=proposal,
        )

    if action == "reject":
        appointment_request.status = (
            AppointmentRequestStatus.REJECTED.value
        )
        appointment_request.tenant_comment = (
            tenant_comment
        )
        appointment_request.responded_at = (
            utc_now()
        )

        audit(
            "appointment.rejected",
            "commission",
            appointment_request.commission_id,
            {
                "appointment_request_id": appointment_request.id,
                "tenant_comment": tenant_comment,
            },
        )

        db.session.commit()

        notify_assigned_user(
            appointment_request,
            None,
        )

        return render_template(
            "appointments/result.html",
            state=AppointmentRequestStatus.REJECTED.value,
            appointment_request=appointment_request,
            accepted_proposal=None,
        )

    return render_template(
        "appointments/respond.html",
        appointment_request=appointment_request,
        error="Die gewünschte Aktion ist ungültig.",
    ), 400