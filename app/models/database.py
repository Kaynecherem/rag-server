"""SQLAlchemy ORM models for the Insurance RAG system."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Text, Boolean, DateTime,
    ForeignKey, JSON, Enum as SAEnum, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship
import enum


class Base(DeclarativeBase):
    pass


# ── Enums ──────────────────────────────────────────────────────────────────

class TenantStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TRIAL = "trial"


class DocumentStatus(str, enum.Enum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class DocumentType(str, enum.Enum):
    POLICY = "policy"
    COMMUNICATION = "communication"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    STAFF = "staff"
    POLICYHOLDER = "policyholder"


# ── Tenants (Insurance Agencies) ──────────────────────────────────────────

class Tenant(Base):
    """An insurance agency using the platform."""
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    status = Column(SAEnum(TenantStatus), default=TenantStatus.TRIAL, nullable=False)
    widget_config = Column(JSON, default=dict)  # branding, colors, welcome msg
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    documents = relationship("Document", back_populates="tenant", cascade="all, delete-orphan")
    staff_users = relationship("StaffUser", back_populates="tenant", cascade="all, delete-orphan")
    policyholders = relationship("Policyholder", back_populates="tenant", cascade="all, delete-orphan")
    query_logs = relationship("QueryLog", back_populates="tenant", cascade="all, delete-orphan")


# ── Documents (Policies + Communications) ─────────────────────────────────

class Document(Base):
    """A document uploaded to the system (policy or communication)."""
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    document_type = Column(SAEnum(DocumentType), nullable=False)
    status = Column(SAEnum(DocumentStatus), default=DocumentStatus.UPLOADING)

    # Policy-specific
    policy_number = Column(String(100), nullable=True, index=True)

    # Communication-specific
    communication_type = Column(String(100), nullable=True)  # letter, agent_note, e_and_o, memo

    # Common metadata
    filename = Column(String(500), nullable=False)
    title = Column(String(500), nullable=True)
    s3_key = Column(String(1000), nullable=False)
    page_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)

    # Processing
    job_id = Column(String(100), unique=True, index=True)
    error_message = Column(Text, nullable=True)
    processed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    tenant = relationship("Tenant", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_documents_tenant_policy", "tenant_id", "policy_number"),
        Index("ix_documents_tenant_type", "tenant_id", "document_type"),
    )


class DocumentChunk(Base):
    """A chunk of text extracted from a document, stored with embedding reference."""
    __tablename__ = "document_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    section_title = Column(String(500), nullable=True)
    token_count = Column(Integer, nullable=True)
    pinecone_id = Column(String(200), nullable=True, index=True)  # Reference to vector DB

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    document = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_document_index", "document_id", "chunk_index"),
    )


# ── Users ──────────────────────────────────────────────────────────────────

class StaffUser(Base):
    """Agency staff member (admin or regular staff)."""
    __tablename__ = "staff_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    auth0_user_id = Column(String(200), unique=True, nullable=False)
    email = Column(String(255), nullable=False)
    name = Column(String(255), nullable=True)
    role = Column(SAEnum(UserRole), default=UserRole.STAFF, nullable=False)
    is_active = Column(Boolean, default=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    tenant = relationship("Tenant", back_populates="staff_users")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_staff_tenant_email"),
    )


class Policyholder(Base):
    """A policyholder linked to a specific policy within a tenant."""
    __tablename__ = "policyholders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    policy_number = Column(String(100), nullable=False)
    last_name = Column(String(255), nullable=True)
    company_name = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    tenant = relationship("Tenant", back_populates="policyholders")

    __table_args__ = (
        Index("ix_policyholders_lookup", "tenant_id", "policy_number", "last_name"),
        Index("ix_policyholders_company", "tenant_id", "policy_number", "company_name"),
    )


# ── Audit / Query Logs ────────────────────────────────────────────────────

class QueryLog(Base):
    """Audit log for every query made against the system."""
    __tablename__ = "query_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    user_type = Column(SAEnum(UserRole), nullable=False)
    user_identifier = Column(String(255), nullable=False)  # email or policy_number
    policy_number = Column(String(100), nullable=True)
    document_type = Column(SAEnum(DocumentType), nullable=True)  # what was queried
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    citations = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)
    retrieval_scores = Column(JSON, nullable=True)  # similarity scores from vector search
    latency_ms = Column(Integer, nullable=True)
    queried_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    tenant = relationship("Tenant", back_populates="query_logs")

    __table_args__ = (
        Index("ix_query_logs_tenant_date", "tenant_id", "queried_at"),
    )
