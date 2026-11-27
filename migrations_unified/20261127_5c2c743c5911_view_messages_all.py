"""view messages_all

Revision ID: 5c2c743c5911
Revises: 
Create Date: 2026-11-27 19:28:32.211476

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c2c743c5911'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create unified.messages_all over the platform schemas that exist now (see strata_takeout.unified.view)."""
    from strata_takeout.unified import view

    view.build(op.get_bind())


def downgrade() -> None:
    """Drop the view."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP VIEW IF EXISTS unified.messages_all")
