"""Add time and material review fields

Revision ID: c1d2e3f4a5b6
Revises: bf1f7c402f2c
Create Date: 2026-07-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c1d2e3f4a5b6'
down_revision = 'bf1f7c402f2c'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'time_task_expected_material',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_template_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Float(), server_default='1', nullable=False),
        sa.Column('required', sa.Boolean(), server_default='1', nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['item_id'], ['inventory_item.id']),
        sa.ForeignKeyConstraint(['task_template_id'], ['time_task_template.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_template_id', 'item_id', name='uq_time_task_expected_material_task_item'),
    )
    with op.batch_alter_table('time_task_expected_material', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_time_task_expected_material_item_id'), ['item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_time_task_expected_material_task_template_id'), ['task_template_id'], unique=False)

    with op.batch_alter_table('inventory_movement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('task_template_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('usage_status', sa.String(length=40), server_default='installed', nullable=False))
        batch_op.add_column(sa.Column('source', sa.String(length=40), server_default='warehouse', nullable=False))
        batch_op.add_column(sa.Column('deviation_reason', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('review_status', sa.String(length=40), server_default='ok', nullable=False))
        batch_op.add_column(sa.Column('review_level', sa.String(length=20), server_default='green', nullable=False))
        batch_op.add_column(sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_inventory_movement_task_template_id'), ['task_template_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movement_usage_status'), ['usage_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movement_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movement_review_status'), ['review_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movement_review_level'), ['review_level'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movement_reviewed_by_user_id'), ['reviewed_by_user_id'], unique=False)
        batch_op.create_foreign_key('fk_inventory_movement_task_template_id_time_task_template', 'time_task_template', ['task_template_id'], ['id'])
        batch_op.create_foreign_key('fk_inventory_movement_reviewed_by_user_id_user_account', 'user_account', ['reviewed_by_user_id'], ['id'])

    with op.batch_alter_table('time_entry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('work_minutes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('waiting_minutes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('material_minutes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('travel_minutes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('documentation_minutes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('extra_minutes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('deviation_reason', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('review_level', sa.String(length=20), server_default='green', nullable=False))
        batch_op.create_index(batch_op.f('ix_time_entry_review_level'), ['review_level'], unique=False)


def downgrade():
    with op.batch_alter_table('time_entry', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_time_entry_review_level'))
        batch_op.drop_column('review_level')
        batch_op.drop_column('deviation_reason')
        batch_op.drop_column('extra_minutes')
        batch_op.drop_column('documentation_minutes')
        batch_op.drop_column('travel_minutes')
        batch_op.drop_column('material_minutes')
        batch_op.drop_column('waiting_minutes')
        batch_op.drop_column('work_minutes')

    with op.batch_alter_table('inventory_movement', schema=None) as batch_op:
        batch_op.drop_constraint('fk_inventory_movement_reviewed_by_user_id_user_account', type_='foreignkey')
        batch_op.drop_constraint('fk_inventory_movement_task_template_id_time_task_template', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_inventory_movement_reviewed_by_user_id'))
        batch_op.drop_index(batch_op.f('ix_inventory_movement_review_level'))
        batch_op.drop_index(batch_op.f('ix_inventory_movement_review_status'))
        batch_op.drop_index(batch_op.f('ix_inventory_movement_source'))
        batch_op.drop_index(batch_op.f('ix_inventory_movement_usage_status'))
        batch_op.drop_index(batch_op.f('ix_inventory_movement_task_template_id'))
        batch_op.drop_column('reviewed_at')
        batch_op.drop_column('reviewed_by_user_id')
        batch_op.drop_column('review_level')
        batch_op.drop_column('review_status')
        batch_op.drop_column('deviation_reason')
        batch_op.drop_column('source')
        batch_op.drop_column('usage_status')
        batch_op.drop_column('task_template_id')

    with op.batch_alter_table('time_task_expected_material', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_time_task_expected_material_task_template_id'))
        batch_op.drop_index(batch_op.f('ix_time_task_expected_material_item_id'))
    op.drop_table('time_task_expected_material')
