"""Policy management routes - upload, status tracking, deletion, download, search."""

import uuid

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query, Response
from sqlalchemy import select, func, or_, asc
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.api.dependencies import require_staff, get_tenant_id, get_current_user
from app.models.database import Document, DocumentType, DocumentStatus, DocumentChunk
from app.models.schemas import (
    PolicyUploadResponse, PolicyStatusResponse,
    PolicyDeleteResponse, PolicyAvailableResponse,
)
from app.services.storage_service import StorageService
from app.services.document_processor import DocumentProcessor
from app.services.embedding_service import EmbeddingService
from app.services.retrieval_service import RetrievalService

logger = structlog.get_logger()
router = APIRouter()


def get_storage():
    return StorageService()

def get_processor():
    return DocumentProcessor()

def get_embedding_service():
    return EmbeddingService()

def get_retrieval_service():
    return RetrievalService()


# ── Search (must come BEFORE /{policy_number} routes) ────────────────

@router.get("/search")
async def search_policies(
    q: str = Query("", min_length=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Search policies by policy number, filename, or title."""
    base_filter = [
        Document.tenant_id == tenant_id,
        Document.document_type == DocumentType.POLICY,
    ]

    if q.strip():
        base_filter.append(
            or_(
                Document.policy_number.ilike(f"%{q}%"),
                Document.filename.ilike(f"%{q}%"),
                Document.title.ilike(f"%{q}%"),
            )
        )

    query = select(Document).where(*base_filter)
    count_query = select(func.count(Document.id)).where(*base_filter)

    total = (await db.execute(count_query)).scalar()

    query = query.order_by(Document.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    docs = result.scalars().all()

    return {
        "policies": [
            {
                "policy_number": doc.policy_number,
                "filename": doc.filename,
                "title": doc.title,
                "status": doc.status.value,
                "page_count": doc.page_count,
                "chunk_count": doc.chunk_count,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
            }
            for doc in docs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Upload ───────────────────────────────────────────────────────────

@router.post("/upload", response_model=PolicyUploadResponse)
async def upload_policy(
    file: UploadFile = File(...),
    policy_number: str = Form(...),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Upload a policy PDF for processing and indexing."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds 50MB limit")

    job_id = f"job_{uuid.uuid4().hex[:12]}"

    doc = Document(
        tenant_id=tenant_id,
        document_type=DocumentType.POLICY,
        status=DocumentStatus.PROCESSING,
        policy_number=policy_number,
        filename=file.filename,
        s3_key="",
        file_size_bytes=len(file_bytes),
        job_id=job_id,
    )
    db.add(doc)
    await db.flush()

    try:
        s3_key = await get_storage().upload_policy(tenant_id, policy_number, file_bytes, file.filename)
        doc.s3_key = s3_key

        processed = get_processor().process_pdf(file_bytes, file.filename)
        doc.page_count = processed.page_count
        doc.chunk_count = len(processed.chunks)

        chunk_texts = [c.text for c in processed.chunks]
        embeddings = await get_embedding_service().embed_texts(chunk_texts)

        chunks_for_pinecone = [
            {
                "chunk_id": c.chunk_id,
                "text": c.text,
                "embedding": emb,
                "page_number": c.page_number,
                "section_title": c.section_title,
                "chunk_index": c.chunk_index,
            }
            for c, emb in zip(processed.chunks, embeddings)
        ]

        await get_retrieval_service().upsert_chunks(
            chunks=chunks_for_pinecone,
            tenant_id=tenant_id,
            document_type="policy",
            policy_number=policy_number,
        )

        for chunk in processed.chunks:
            db.add(DocumentChunk(
                document_id=doc.id,
                chunk_index=chunk.chunk_index,
                chunk_text=chunk.text,
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                token_count=chunk.token_count,
                pinecone_id=chunk.chunk_id,
            ))

        doc.status = DocumentStatus.INDEXED
        from datetime import datetime
        doc.processed_at = datetime.utcnow()

        logger.info("Policy indexed",
            policy_number=policy_number,
            chunks=len(processed.chunks),
            pages=processed.page_count,
        )

    except Exception as e:
        doc.status = DocumentStatus.FAILED
        doc.error_message = str(e)
        logger.error("Policy processing failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

    await db.commit()

    return PolicyUploadResponse(
        job_id=job_id,
        policy_number=policy_number,
        status=doc.status.value,
        page_count=doc.page_count,
        chunk_count=doc.chunk_count,
    )


# ── Status & Availability ───────────────────────────────────────────

@router.get("/{policy_number}/available", response_model=PolicyAvailableResponse)
async def check_policy_available(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Check if a policy has been indexed and is available for queries."""
    if current_user.get("role") == "policyholder":
        if current_user.get("sub") != policy_number:
            raise HTTPException(status_code=403, detail="Access denied to this policy")

    result = await db.execute(
        select(Document).where(
            Document.policy_number == policy_number,
            Document.tenant_id == tenant_id,
            Document.document_type == DocumentType.POLICY,
        )
    )
    doc = result.scalar_one_or_none()

    return PolicyAvailableResponse(
        available=doc is not None and doc.status == DocumentStatus.INDEXED,
        policy_number=policy_number,
        indexed_at=doc.processed_at if doc else None,
        chunk_count=doc.chunk_count if doc else None,
    )


# ── Download & Text ─────────────────────────────────────────────────

@router.get("/{policy_number}/download")
async def download_policy(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Download the original policy PDF."""
    if user.get("role") == "policyholder":
        if user.get("sub") != policy_number:
            raise HTTPException(status_code=403, detail="Access denied to this policy")

    result = await db.execute(
        select(Document).where(
            Document.policy_number == policy_number,
            Document.tenant_id == tenant_id,
            Document.document_type == DocumentType.POLICY,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Policy not found")

    try:
        storage = get_storage()
        file_bytes = await storage.download_file(doc.s3_key)
    except Exception as e:
        logger.error("Download failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve policy file")

    return Response(
        content=file_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{doc.filename}"',
        },
    )


@router.get("/{policy_number}/text")
async def get_policy_text(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Get the extracted text of a policy (for in-browser reading)."""
    if user.get("role") == "policyholder":
        if user.get("sub") != policy_number:
            raise HTTPException(status_code=403, detail="Access denied to this policy")

    result = await db.execute(
        select(Document).where(
            Document.policy_number == policy_number,
            Document.tenant_id == tenant_id,
            Document.document_type == DocumentType.POLICY,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Policy not found")

    chunk_result = await db.execute(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == doc.id)
        .order_by(asc(DocumentChunk.chunk_index))
    )
    chunks = chunk_result.scalars().all()

    sections = []
    for chunk in chunks:
        sections.append({
            "page": chunk.page_number,
            "section": chunk.section_title,
            "text": chunk.chunk_text,
        })

    return {
        "policy_number": policy_number,
        "filename": doc.filename,
        "page_count": doc.page_count,
        "sections": sections,
    }


# ── Delete ───────────────────────────────────────────────────────────

@router.delete("/{policy_number}", response_model=PolicyDeleteResponse)
async def delete_policy_endpoint(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Delete a policy and all associated data."""
    result = await db.execute(
        select(Document).where(
            Document.policy_number == policy_number,
            Document.tenant_id == tenant_id,
            Document.document_type == DocumentType.POLICY,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Policy not found")

    # Delete chunks from Pinecone
    chunk_result = await db.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
    )
    chunks = chunk_result.scalars().all()
    chunk_ids = [c.pinecone_id for c in chunks if c.pinecone_id]

    if chunk_ids:
        try:
            await get_retrieval_service().delete_document_vectors(tenant_id, chunk_ids)
        except Exception as e:
            logger.warning("Failed to delete vectors", error=str(e))

    # Delete from storage
    try:
        await get_storage().delete_policy(tenant_id, policy_number)
    except Exception as e:
        logger.warning("Failed to delete from storage", error=str(e))

    # Delete DB records
    for chunk in chunks:
        await db.delete(chunk)
    await db.delete(doc)
    await db.commit()

    return PolicyDeleteResponse(
        policy_number=policy_number,
        status="deleted",
    )