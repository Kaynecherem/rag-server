"""Query History routes - browse past questions and answers."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.api.dependencies import get_current_user, require_staff, get_tenant_id
from app.models.database import QueryLog, UserRole, DocumentType

logger = structlog.get_logger()
router = APIRouter()


# ── Staff: View all query history for their tenant ─────────────────────────

@router.get("/staff")
async def list_staff_query_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user_type: str | None = Query(None, description="Filter by user type: staff, policyholder"),
    document_type: str | None = Query(None, description="Filter by doc type: policy, communication"),
    policy_number: str | None = Query(None, description="Filter by policy number"),
    search: str | None = Query(None, description="Search in questions"),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Staff view: List all query history across the tenant.
    Supports filtering by user type, document type, policy number, and text search.
    """
    base_filter = [QueryLog.tenant_id == tenant_id]

    if user_type:
        try:
            base_filter.append(QueryLog.user_type == UserRole(user_type))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid user_type: {user_type}")

    if document_type:
        try:
            base_filter.append(QueryLog.document_type == DocumentType(document_type))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid document_type: {document_type}")

    if policy_number:
        base_filter.append(QueryLog.policy_number == policy_number)

    if search:
        base_filter.append(QueryLog.question.ilike(f"%{search}%"))

    # Total count
    count_q = select(func.count(QueryLog.id)).where(*base_filter)
    total = (await db.execute(count_q)).scalar()

    # Paginated results
    query = (
        select(QueryLog)
        .where(*base_filter)
        .order_by(desc(QueryLog.queried_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    logs = result.scalars().all()

    return {
        "queries": [_format_query_log(log) for log in logs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/staff/stats")
async def query_history_stats(
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Staff view: Aggregate stats about query history.
    Total queries, by user type, avg confidence, avg latency.
    """
    base = [QueryLog.tenant_id == tenant_id]

    total = (await db.execute(
        select(func.count(QueryLog.id)).where(*base)
    )).scalar()

    staff_count = (await db.execute(
        select(func.count(QueryLog.id)).where(*base, QueryLog.user_type == UserRole.STAFF)
    )).scalar()

    policyholder_count = (await db.execute(
        select(func.count(QueryLog.id)).where(*base, QueryLog.user_type == UserRole.POLICYHOLDER)
    )).scalar()

    avg_confidence = (await db.execute(
        select(func.avg(QueryLog.confidence)).where(*base)
    )).scalar()

    avg_latency = (await db.execute(
        select(func.avg(QueryLog.latency_ms)).where(*base)
    )).scalar()

    policy_queries = (await db.execute(
        select(func.count(QueryLog.id)).where(*base, QueryLog.document_type == DocumentType.POLICY)
    )).scalar()

    comm_queries = (await db.execute(
        select(func.count(QueryLog.id)).where(*base, QueryLog.document_type == DocumentType.COMMUNICATION)
    )).scalar()

    return {
        "total_queries": total,
        "by_user_type": {
            "staff": staff_count,
            "policyholder": policyholder_count,
        },
        "by_document_type": {
            "policy": policy_queries,
            "communication": comm_queries,
        },
        "avg_confidence": round(avg_confidence or 0, 4),
        "avg_latency_ms": round(avg_latency or 0),
    }


# ── Staff: View a single query detail ─────────────────────────────────────

@router.get("/staff/{query_id}")
async def get_query_detail(
    query_id: str,
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Staff view: Get full detail for a single past query."""
    result = await db.execute(
        select(QueryLog).where(
            QueryLog.id == query_id,
            QueryLog.tenant_id == tenant_id,
        )
    )
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Query not found")

    return _format_query_log(log, include_full=True)


# ── Policyholder: View their own query history ─────────────────────────────

@router.get("/policyholder")
async def list_policyholder_query_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Policyholder view: List their own past queries.
    Scoped to their policy number only.
    """
    if current_user.get("role") != "policyholder":
        raise HTTPException(status_code=403, detail="This endpoint is for policyholders only")

    policy_number = current_user.get("sub")
    if not policy_number:
        raise HTTPException(status_code=403, detail="No policy number in session")

    base_filter = [
        QueryLog.tenant_id == tenant_id,
        QueryLog.user_type == UserRole.POLICYHOLDER,
        QueryLog.policy_number == policy_number,
    ]

    total = (await db.execute(
        select(func.count(QueryLog.id)).where(*base_filter)
    )).scalar()

    query = (
        select(QueryLog)
        .where(*base_filter)
        .order_by(desc(QueryLog.queried_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    logs = result.scalars().all()

    return {
        "queries": [_format_query_log(log) for log in logs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _format_query_log(log: QueryLog, include_full: bool = False) -> dict:
    """Format a QueryLog row for API response."""
    data = {
        "id": str(log.id),
        "user_type": log.user_type.value if log.user_type else None,
        "user_identifier": log.user_identifier,
        "policy_number": log.policy_number,
        "document_type": log.document_type.value if log.document_type else None,
        "question": log.question,
        "confidence": log.confidence,
        "latency_ms": log.latency_ms,
        "queried_at": log.queried_at.isoformat() if log.queried_at else None,
    }

    if include_full:
        data["answer"] = log.answer
        data["citations"] = log.citations
        data["retrieval_scores"] = log.retrieval_scores

    else:
        # Truncated answer for list view
        data["answer_preview"] = (log.answer or "")[:200]
        data["citation_count"] = len(log.citations) if log.citations else 0

    return data