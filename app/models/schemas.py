"""Pydantic schemas for API request/response validation."""

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field


# ── Policy Schemas ─────────────────────────────────────────────────────────

class PolicyUploadResponse(BaseModel):
    job_id: str
    status: str
    policy_number: str
    message: str = "Document uploaded and queued for processing"


class PolicyStatusResponse(BaseModel):
    job_id: str
    status: str  # uploading, processing, indexed, failed
    policy_number: str | None = None
    progress: float | None = None  # 0.0 to 1.0
    page_count: int | None = None
    chunk_count: int | None = None
    error: str | None = None


class PolicyDeleteResponse(BaseModel):
    policy_number: str
    deleted: bool
    message: str


class PolicyAvailableResponse(BaseModel):
    available: bool
    policy_number: str
    indexed_at: datetime | None = None
    chunk_count: int | None = None


# ── Communication Schemas ──────────────────────────────────────────────────

class CommunicationUploadResponse(BaseModel):
    doc_id: str
    job_id: str
    status: str
    communication_type: str
    message: str = "Communication uploaded and queued for processing"


class CommunicationListItem(BaseModel):
    doc_id: str
    filename: str
    communication_type: str
    title: str | None = None
    status: str
    page_count: int | None = None
    created_at: datetime


class CommunicationListResponse(BaseModel):
    communications: list[CommunicationListItem]
    total: int
    page: int
    page_size: int


# ── Query Schemas ──────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)


class Citation(BaseModel):
    page: int | None = None
    section: str | None = None
    text: str
    chunk_id: str | None = None
    similarity_score: float | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    confidence: float
    query_id: str
    latency_ms: int


class CommunicationQueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)
    communication_type: str | None = None  # filter by type


# ── Auth Schemas ───────────────────────────────────────────────────────────

class PolicyholderVerifyRequest(BaseModel):
    """Policyholder verifies identity with policy ID + last name or company name."""
    tenant_id: str
    policy_number: str = Field(..., min_length=1)
    last_name: str | None = None
    company_name: str | None = None

    def model_post_init(self, __context):
        if not self.last_name and not self.company_name:
            raise ValueError("Either last_name or company_name must be provided")


class PolicyholderVerifyResponse(BaseModel):
    verified: bool
    token: str | None = None
    policy_number: str
    expires_at: datetime | None = None
    message: str


class StaffTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: str
    role: str


# ── Widget Schemas ─────────────────────────────────────────────────────────

class WidgetConfigResponse(BaseModel):
    tenant_id: str
    tenant_name: str
    theme: dict = {}
    welcome_message: str = "Hello! Ask me anything about your insurance policy."
    placeholder: str = "Type your question here..."


class WidgetQueryRequest(BaseModel):
    policy_number: str
    question: str = Field(..., min_length=3, max_length=2000)
    session_token: str


# ── Tenant Schemas ─────────────────────────────────────────────────────────

class TenantCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    slug: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")


class TenantResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    created_at: datetime
