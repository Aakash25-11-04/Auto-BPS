"""crew & resource capacity (Feature 10): department_capacity, gang_assignments

Revision ID: b7c1d2e3f4a5
Revises: a1b2c3d4e5f6
Create Date: 2026-09-13 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1d2e3f4a5'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('department_capacity',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('department', sa.String(), nullable=False),
    sa.Column('date', sa.Date(), nullable=True),
    sa.Column('max_concurrent_gangs', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('gang_assignments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('plan_id', sa.String(), nullable=False),
    sa.Column('task_id', sa.String(), nullable=False),
    sa.Column('department', sa.String(), nullable=True),
    sa.Column('gang_id', sa.String(), nullable=False),
    sa.Column('corridor_id', sa.String(), nullable=True),
    sa.Column('assigned_window_start', sa.DateTime(), nullable=False),
    sa.Column('assigned_window_end', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_gang_assignments_plan_id'), 'gang_assignments', ['plan_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_gang_assignments_plan_id'), table_name='gang_assignments')
    op.drop_table('gang_assignments')
    op.drop_table('department_capacity')
