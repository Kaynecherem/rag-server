"""Widget routes - Public endpoints for the embeddable chat widget."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.core.security import verify_policyholder_token
from app.models.database import Tenant, QueryLog, UserRole, DocumentType
from app.models.schemas import WidgetConfigResponse, WidgetQueryRequest, QueryResponse
from app.services.query_orchestrator import QueryOrchestrator

logger = structlog.get_logger()
router = APIRouter()


def get_orchestrator():
    return QueryOrchestrator()


@router.get("/{tenant_id}/config", response_model=WidgetConfigResponse)
async def get_widget_config(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint: Get widget configuration for a tenant.
    Returns branding, theme, and welcome message.
    Called when the widget JS loads on the client's website.
    """
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    config = tenant.widget_config or {}

    return WidgetConfigResponse(
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        theme=config.get("theme", {}),
        welcome_message=config.get(
            "welcome_message",
            "Hello! Ask me anything about your insurance policy."
        ),
        placeholder=config.get("placeholder", "Type your question here..."),
    )


@router.post("/{tenant_id}/query", response_model=QueryResponse)
async def widget_query(
    tenant_id: str,
    request: WidgetQueryRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Widget query endpoint. Requires a valid policyholder session token.
    Rate limited per IP + tenant to prevent abuse.
    """
    # Verify the session token
    claims = verify_policyholder_token(request.session_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Ensure token tenant matches URL tenant
    if claims.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Token does not match tenant")

    # Ensure policyholder can only query their own policy
    if claims.get("sub") != request.policy_number:
        raise HTTPException(status_code=403, detail="Access denied to this policy")

    # Execute RAG query
    result = await get_orchestrator().query_policy(
        question=request.question,
        tenant_id=tenant_id,
        policy_number=request.policy_number,
    )

    # Audit log
    log_entry = QueryLog(
        tenant_id=tenant_id,
        user_type=UserRole.POLICYHOLDER,
        user_identifier=request.policy_number,
        policy_number=request.policy_number,
        document_type=DocumentType.POLICY,
        question=request.question,
        answer=result["answer"],
        citations=result["citations"],
        confidence=result["confidence"],
        latency_ms=result["latency_ms"],
    )
    db.add(log_entry)

    return QueryResponse(**result)