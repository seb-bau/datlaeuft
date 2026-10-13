from alembic import op
import sqlalchemy as sa


revision = "a1c3e5f7b9d2"
down_revision = "f0a2c4e6b8d1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "safety_inspection_template",
        sa.Column(
            "used_for_jobs",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    templates = sa.table(
        "safety_inspection_template",
        sa.column("id", sa.Integer()),
        sa.column("used_for_jobs", sa.Boolean()),
    )
    versions = sa.table(
        "safety_inspection_template_version",
        sa.column("id", sa.Integer()),
        sa.column("template_id", sa.Integer()),
    )
    jobs = sa.table(
        "safety_inspection_job",
        sa.column("template_version_id", sa.Integer()),
    )

    used_template_ids = (
        sa.select(versions.c.template_id)
        .select_from(versions)
        .join(jobs, jobs.c.template_version_id == versions.c.id)
    )

    op.get_bind().execute(
        templates.update()
        .where(templates.c.id.in_(used_template_ids))
        .values(used_for_jobs=True)
    )


def downgrade():
    with op.batch_alter_table("safety_inspection_template") as batch_op:
        batch_op.drop_column("used_for_jobs")