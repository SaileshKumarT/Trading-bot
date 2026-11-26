"""Add sl_order_id and tp_order_id columns to trades table.

Stores the exchange order IDs for SL/TP orders directly on the trade
record so the executor can quickly cancel them when a trade closes.

Revision ID: 0002_sl_tp_ids
Revises: 0001_initial
Create Date: 2025-03-31 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_sl_tp_ids"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sl_order_id and tp_order_id to the trades table."""
    op.add_column("trades", sa.Column("sl_order_id", sa.String(100), nullable=True))
    op.add_column("trades", sa.Column("tp_order_id", sa.String(100), nullable=True))


def downgrade() -> None:
    """Remove sl_order_id and tp_order_id from the trades table."""
    op.drop_column("trades", "tp_order_id")
    op.drop_column("trades", "sl_order_id")
