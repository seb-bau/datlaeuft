from alembic import op


revision = "d3a4b5c6d7e8"
down_revision = "c2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "commission_appointment_proposal",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_appointment_proposal_accepted"
        )
        batch_op.drop_index(
            "ix_appointment_proposal_date"
        )
        batch_op.drop_index(
            "ix_appointment_proposal_request_id"
        )
        batch_op.drop_index(
            "ix_appointment_proposal_window_id"
        )

        batch_op.create_index(
            "ix_commission_appointment_proposal_accepted",
            ["accepted"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_proposal_proposed_date",
            ["proposed_date"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_proposal_appointment_request_id",
            ["appointment_request_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_proposal_window_id",
            ["window_id"],
            unique=False,
        )

    with op.batch_alter_table(
        "commission_appointment_request",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_appointment_request_commission_id"
        )
        batch_op.drop_index(
            "ix_appointment_request_created_at"
        )
        batch_op.drop_index(
            "ix_appointment_request_created_by_user_id"
        )
        batch_op.drop_index(
            "ix_appointment_request_expires_at"
        )
        batch_op.drop_index(
            "ix_appointment_request_recipient_email"
        )
        batch_op.drop_index(
            "ix_appointment_request_status"
        )
        batch_op.drop_index(
            "ix_appointment_request_token_hash"
        )

        batch_op.create_index(
            "ix_commission_appointment_request_commission_id",
            ["commission_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_request_created_at",
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_request_created_by_user_id",
            ["created_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_request_expires_at",
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_request_recipient_email",
            ["recipient_email"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_request_status",
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_appointment_request_token_hash",
            ["token_hash"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table(
        "commission_appointment_request",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_commission_appointment_request_token_hash"
        )
        batch_op.drop_index(
            "ix_commission_appointment_request_status"
        )
        batch_op.drop_index(
            "ix_commission_appointment_request_recipient_email"
        )
        batch_op.drop_index(
            "ix_commission_appointment_request_expires_at"
        )
        batch_op.drop_index(
            "ix_commission_appointment_request_created_by_user_id"
        )
        batch_op.drop_index(
            "ix_commission_appointment_request_created_at"
        )
        batch_op.drop_index(
            "ix_commission_appointment_request_commission_id"
        )

        batch_op.create_index(
            "ix_appointment_request_token_hash",
            ["token_hash"],
            unique=True,
        )
        batch_op.create_index(
            "ix_appointment_request_status",
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_request_recipient_email",
            ["recipient_email"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_request_expires_at",
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_request_created_by_user_id",
            ["created_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_request_created_at",
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_request_commission_id",
            ["commission_id"],
            unique=False,
        )

    with op.batch_alter_table(
        "commission_appointment_proposal",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_commission_appointment_proposal_window_id"
        )
        batch_op.drop_index(
            "ix_commission_appointment_proposal_appointment_request_id"
        )
        batch_op.drop_index(
            "ix_commission_appointment_proposal_proposed_date"
        )
        batch_op.drop_index(
            "ix_commission_appointment_proposal_accepted"
        )

        batch_op.create_index(
            "ix_appointment_proposal_window_id",
            ["window_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_proposal_request_id",
            ["appointment_request_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_proposal_date",
            ["proposed_date"],
            unique=False,
        )
        batch_op.create_index(
            "ix_appointment_proposal_accepted",
            ["accepted"],
            unique=False,
        )