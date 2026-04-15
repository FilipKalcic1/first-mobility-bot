"""Align database schema with ORM models

Revision ID: 003_align_orm
Revises: 002_gdpr_consent
Create Date: 2026-03-26

Fixes mismatches between 001_initial migration and current ORM models:
- conversations: add ended_at, status, flow_type, metadata; drop last_activity, message_count, state
- messages: add timestamp, tool_name, tool_call_id, tool_result; drop created_at, metadata
- tool_executions: drop conversation_id FK (ORM doesn't define it)
- All DateTime columns: upgrade to timezone-aware
- user_mappings: upgrade created_at/updated_at to timezone-aware
- Recreate indexes to match ORM __table_args__

WARNING — DATA-DESTRUCTIVE MIGRATION
------------------------------------
This migration DROPS production columns after a best-effort UPDATE copy:
  conversations: last_activity, message_count, state
  messages:     created_at, metadata
  tool_executions: conversation_id (and its FK)

The copy into ended_at/status and timestamp is NOT transactional with the
drops across all rows under concurrent writes. Before running:

  1) Take a full logical backup (`pg_dump`) of the target database.
  2) Run in a maintenance window with application writes paused, OR run on
     a staging clone first and diff row counts.
  3) Verify `downgrade()` on that staging clone — it restores columns as
     NULL, so downgrade is NOT a full undo of lost data.

Do NOT run this in production without the above checklist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = '003_align_orm'
down_revision: Union[str, None] = '002_gdpr_consent'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # === CONVERSATIONS: align with ORM ===
    # Add new columns
    op.add_column('conversations', sa.Column(
        'ended_at', sa.DateTime(timezone=True), nullable=True
    ))
    op.add_column('conversations', sa.Column(
        'status', sa.String(20), server_default='active'
    ))
    op.add_column('conversations', sa.Column(
        'flow_type', sa.String(50), nullable=True
    ))
    op.add_column('conversations', sa.Column(
        'metadata', postgresql.JSON(), nullable=True
    ))

    # Migrate data from old columns to new ones
    op.execute("""
        UPDATE conversations SET
            ended_at = last_activity,
            status = COALESCE(state, 'active')
        WHERE last_activity IS NOT NULL OR state IS NOT NULL
    """)

    # Drop old columns
    op.drop_index('ix_conv_activity', table_name='conversations')
    op.drop_column('conversations', 'last_activity')
    op.drop_column('conversations', 'message_count')
    op.drop_column('conversations', 'state')

    # Create new composite index matching ORM
    op.create_index('ix_conv_user_status', 'conversations', ['user_id', 'status'])

    # Make started_at timezone-aware
    op.alter_column('conversations', 'started_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)

    # === MESSAGES: align with ORM ===
    # Add new columns
    op.add_column('messages', sa.Column(
        'timestamp', sa.DateTime(timezone=True), nullable=True
    ))
    op.add_column('messages', sa.Column(
        'tool_name', sa.String(100), nullable=True
    ))
    op.add_column('messages', sa.Column(
        'tool_call_id', sa.String(100), nullable=True
    ))
    op.add_column('messages', sa.Column(
        'tool_result', postgresql.JSON(), nullable=True
    ))

    # Migrate created_at -> timestamp
    op.execute("UPDATE messages SET timestamp = created_at WHERE created_at IS NOT NULL")

    # Drop old columns and indexes
    op.drop_index('ix_msg_created', table_name='messages')
    op.drop_column('messages', 'created_at')
    op.drop_column('messages', 'metadata')

    # Recreate index matching ORM
    op.create_index('ix_msg_conv_time', 'messages', ['conversation_id', 'timestamp'])

    # === TOOL_EXECUTIONS: drop conversation_id FK ===
    # ORM doesn't define this FK; remove it to align
    op.drop_constraint(
        'tool_executions_conversation_id_fkey', 'tool_executions', type_='foreignkey'
    )
    op.drop_column('tool_executions', 'conversation_id')

    # Make executed_at timezone-aware
    op.alter_column('tool_executions', 'executed_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)

    # === USER_MAPPINGS: make timestamps timezone-aware ===
    op.alter_column('user_mappings', 'created_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)
    op.alter_column('user_mappings', 'updated_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)

    # Create composite index matching ORM
    op.create_index('ix_user_phone_active', 'user_mappings', ['phone_number', 'is_active'])

    # === AUDIT_LOGS: make timestamps timezone-aware ===
    op.alter_column('audit_logs', 'created_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)

    # === HALLUCINATION_REPORTS: make timestamps timezone-aware ===
    op.alter_column('hallucination_reports', 'created_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)
    op.alter_column('hallucination_reports', 'reviewed_at',
                     type_=sa.DateTime(timezone=True),
                     existing_type=sa.DateTime(),
                     existing_nullable=True)

    # Grant permissions for new columns
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversations TO bot_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON messages TO bot_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tool_executions TO bot_user")


def downgrade() -> None:
    # === HALLUCINATION_REPORTS ===
    op.alter_column('hallucination_reports', 'reviewed_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))
    op.alter_column('hallucination_reports', 'created_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))

    # === AUDIT_LOGS ===
    op.alter_column('audit_logs', 'created_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))

    # === USER_MAPPINGS ===
    op.drop_index('ix_user_phone_active', table_name='user_mappings')
    op.alter_column('user_mappings', 'updated_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))
    op.alter_column('user_mappings', 'created_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))

    # === TOOL_EXECUTIONS ===
    op.alter_column('tool_executions', 'executed_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))
    op.add_column('tool_executions', sa.Column(
        'conversation_id', postgresql.UUID(as_uuid=True), nullable=True
    ))
    op.create_foreign_key(
        'tool_executions_conversation_id_fkey', 'tool_executions',
        'conversations', ['conversation_id'], ['id'], ondelete='SET NULL'
    )

    # === MESSAGES ===
    op.add_column('messages', sa.Column('metadata', postgresql.JSON(), nullable=True))
    op.add_column('messages', sa.Column('created_at', sa.DateTime(), nullable=True))
    op.execute("UPDATE messages SET created_at = timestamp WHERE timestamp IS NOT NULL")
    op.drop_index('ix_msg_conv_time', table_name='messages')
    op.create_index('ix_msg_created', 'messages', ['created_at'])
    op.drop_column('messages', 'tool_result')
    op.drop_column('messages', 'tool_call_id')
    op.drop_column('messages', 'tool_name')
    op.drop_column('messages', 'timestamp')

    # === CONVERSATIONS ===
    op.add_column('conversations', sa.Column('state', sa.String(50), nullable=True))
    op.add_column('conversations', sa.Column('message_count', sa.Integer(), nullable=True))
    op.add_column('conversations', sa.Column('last_activity', sa.DateTime(), nullable=True))
    op.execute("""
        UPDATE conversations SET
            last_activity = ended_at,
            state = COALESCE(status, 'active')
    """)
    op.drop_index('ix_conv_user_status', table_name='conversations')
    op.create_index('ix_conv_activity', 'conversations', ['last_activity'])
    op.drop_column('conversations', 'metadata')
    op.drop_column('conversations', 'flow_type')
    op.drop_column('conversations', 'status')
    op.drop_column('conversations', 'ended_at')
    op.alter_column('conversations', 'started_at',
                     type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))
