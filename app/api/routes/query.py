"""Query routes - RAG query endpoints for policies and communications."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.api.dependencies import get_current_user, require_staff, get_tenant_id
from app.models.schemas import QueryRequest, QueryResponse, CommunicationQueryRequest
from app.models.database import QueryLog, UserRole, DocumentType
from app.services.query_orchestrator import QueryOrchestrator

logger = structlog.get_logger()
router = APIRouter()

orchestrator = QueryOrchestrator()


@router.post("/policies/{policy_number}/query", response_model=QueryResponse)
async def query_policy(
    policy_number: str,
    request: QueryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Ask a question about a specific insurance policy.

    Accessible by:
    - Staff: can query any policy within their tenant
    - Policyholders: can only query their own policy
    """
    # Policyholder access control
    if current_user.get("role") == "policyholder":
        if current_user.get("sub") != policy_number:
            raise HTTPException(status_code=403, detail="You can only query your own policy")

    # Execute RAG pipeline
    result = await orchestrator.query_policy(
        question=request.question,
        tenant_id=tenant_id,
        policy_number=policy_number,
    )

    # Log the query for audit
    log_entry = QueryLog(
        tenant_id=tenant_id,
        user_type=UserRole(current_user.get("role", "policyholder")),
        user_identifier=current_user.get("email", current_user.get("sub", "unknown")),
        policy_number=policy_number,
        document_type=DocumentType.POLICY,
        question=request.question,
        answer=result["answer"],
        citations=result["citations"],
        confidence=result["confidence"],
        latency_ms=result["latency_ms"],
    )
    db.add(log_entry)

    return QueryResponse(**result)


@router.post("/communications/query", response_model=QueryResponse)
async def query_communications(
    request: CommunicationQueryRequest,
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Ask a question across agency communications (letters, notes, E&O records).

    Staff only - policyholders cannot access the communication bucket.
    """
    result = await orchestrator.query_communications(
        question=request.question,
        tenant_id=tenant_id,
        communication_type=request.communication_type,
    )

    # Audit log
    log_entry = QueryLog(
        tenant_id=tenant_id,
        user_type=UserRole(staff.get("role", "staff")),
        user_identifier=staff.get("email", "unknown"),
        document_type=DocumentType.COMMUNICATION,
        question=request.question,
        answer=result["answer"],
        citations=result["citations"],
        confidence=result["confidence"],
        latency_ms=result["latency_ms"],
    )
    db.add(log_entry)

    return QueryResponse(**result)
