"""
Database Models

SQLAlchemy ORM models with timezone-aware timestamps.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    Text,
    Integer,
    ForeignKey,
    Index,
    JSON
)
from sqlalchemy.dialects.postgresql import UUID

from base import Base  # Use shared Base to avoid circular imports


def utc_now():
    """Return current UTC time with timezone info (not deprecated)."""
    return datetime.now(timezone.utc)


class UserMapping(Base):
    """Maps phone numbers to MobilityOne person IDs."""

    __tablename__ = "user_mappings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    api_identity = Column(String(100), nullable=True, index=True)
    display_name = Column(String(200), nullable=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    # GDPR consent tracking
    gdpr_consent_given = Column(Boolean, default=False)
    gdpr_consent_at = Column(DateTime(timezone=True), nullable=True)
    gdpr_data_retention_days = Column(Integer, default=365)
    gdpr_anonymized_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_user_phone_active", "phone_number", "is_active"),
    )


class Conversation(Base):
    """Conversation metadata."""

    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user_mappings.id"), nullable=False)
    started_at = Column(DateTime(timezone=True), default=utc_now)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), default="active")
    flow_type = Column(String(50), nullable=True)
    metadata_ = Column("metadata", JSON, default=dict)

    __table_args__ = (
        Index("ix_conv_user_status", "user_id", "status"),
    )


class Message(Base):
    """Individual messages."""

    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=utc_now)
    tool_name = Column(String(100), nullable=True)
    tool_call_id = Column(String(100), nullable=True)
    tool_result = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_msg_conv_time", "conversation_id", "timestamp"),
    )


class ToolExecution(Base):
    """Tool execution logs."""

    __tablename__ = "tool_executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tool_name = Column(String(100), nullable=False, index=True)
    parameters = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    execution_time_ms = Column(Integer, nullable=True)
    executed_at = Column(DateTime(timezone=True), default=utc_now)


class AuditLog(Base):
    """
    Audit trail for admin actions.

    SECURITY: This table is ONLY accessible by admin_user.
    Bot/API cannot SELECT from this table.
    """

    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NOTE: user_id is String (not UUID FK) for flexibility
    # Admin ID is stored here, actual details in `details` JSON
    user_id = Column(String(100), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(100), nullable=True)
    entity_id = Column(String(100), nullable=True)
    details = Column(JSON, nullable=True)  # Contains admin_id, ip_address, etc.
    created_at = Column(DateTime(timezone=True), default=utc_now, index=True)

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )


class HallucinationReport(Base):
    """
    Hallucination reports from users ("krivo" feedback).

    Stored in DB for:
    - Long-term analytics
    - Full-text search
    - Compliance/audit requirements
    - Model fine-tuning data export
    """

    __tablename__ = "hallucination_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # What happened
    user_query = Column(Text, nullable=False)
    bot_response = Column(Text, nullable=False)
    user_feedback = Column(Text, nullable=False)

    # Context
    conversation_id = Column(String(50), nullable=True, index=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    model = Column(String(50), nullable=True)
    retrieved_chunks = Column(JSON, default=list)
    api_raw_response = Column(JSON, nullable=True)

    # Review status
    reviewed = Column(Boolean, default=False, index=True)
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    correction = Column(Text, nullable=True)
    category = Column(String(50), nullable=True, index=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=utc_now, index=True)

    __table_args__ = (
        Index("ix_halluc_unreviewed", "reviewed", "created_at"),
        Index("ix_halluc_tenant_cat", "tenant_id", "category"),
    )
