"""chaos

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCENARIOS = ('duplicate_invoice', 'price_spike', 'date_format', 'demand_spike', 'paid_before_reminder',
             'missing_bank_day', 'short_delivery', 'cash_crunch')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('chaos_injection',
    sa.Column('scenario', sa.Enum(*SCENARIOS, native_enum=False), nullable=False),
    sa.Column('injected_by', sa.Uuid(), nullable=True),
    sa.Column('injected_at', sa.DateTime(), nullable=False),
    sa.Column('parameters', sa.JSON(), nullable=False),
    sa.Column('affected_refs', sa.JSON(), nullable=False),
    sa.Column('status', sa.Enum('running', 'completed', 'failed', native_enum=False), nullable=False),
    sa.Column('detected', sa.Boolean(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=True),
    sa.Column('rule_id', sa.Uuid(), nullable=True),
    sa.Column('elapsed_seconds', sa.Float(), nullable=True),
    sa.Column('outcome', sa.JSON(), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('business_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['business.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('chaos_injection', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chaos_injection_business_id'), ['business_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chaos_injection_scenario'), ['scenario'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('chaos_injection', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chaos_injection_scenario'))
        batch_op.drop_index(batch_op.f('ix_chaos_injection_business_id'))
    op.drop_table('chaos_injection')
