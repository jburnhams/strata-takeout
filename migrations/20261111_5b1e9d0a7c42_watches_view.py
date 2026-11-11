"""watches view: real watches from the history file (ads and the overlapping My Activity copy left out)

Revision ID: 5b1e9d0a7c42
Revises: c3cf44ea7b07
"""

from alembic import op

revision = "5b1e9d0a7c42"
down_revision = "c3cf44ea7b07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE VIEW watches AS SELECT id, account, at, video_id, title, channel_id, channel_name, url FROM activity WHERE source = 'watch-history' AND action = 'Watched' AND NOT is_ad"
    )


def downgrade() -> None:
    op.execute("DROP VIEW watches")
