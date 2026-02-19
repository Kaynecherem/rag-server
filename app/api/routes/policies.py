"""Policy management routes - upload, status tracking, deletion."""

import uuid

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.api.dependencies import require_staff, get_tenant_id, get_current_user
from app.models.database import Document, DocumentType, DocumentStatus
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


@router.post("/upload", response_model=PolicyUploadResponse)
async def upload_policy(
    file: UploadFile = File(...),
    policy_number: str = Form(...),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Upload a policy PDF for processing and indexing.

    In production, processing would be async via Celery.
    For MVP, we process synchronously.
    """
    # Validate file
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(status_code=400, detail="File size exceeds 50MB limit")

    job_id = f"job_{uuid.uuid4().hex[:12]}"

    # Create document record
    doc = Document(
        tenant_id=tenant_id,
        document_type=DocumentType.POLICY,
        status=DocumentStatus.PROCESSING,
        policy_number=policy_number,
        filename=file.filename,
        s3_key="",  # Will be set after upload
        file_size_bytes=len(file_bytes),
        job_id=job_id,
    )
    db.add(doc)
    await db.flush()

    try:
        # Step 1: Upload to S3
        s3_key = await get_storage().upload_policy(tenant_id, policy_number, file_bytes, file.filename)
        doc.s3_key = s3_key

        # Step 2: Extract text and chunk
        processed = get_processor().process_pdf(file_bytes, file.filename)
        doc.page_count = processed.page_count
        doc.chunk_count = len(processed.chunks)

        # Step 3: Generate embeddings
        chunk_texts = [c.text for c in processed.chunks]
        embeddings = await get_embedding_service().embed_texts(chunk_texts)

        # Step 4: Store in Pinecone
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

        # Step 5: Save chunk records in DB
        from app.models.database import DocumentChunk
        for chunk in processed.chunks:
            db_chunk = DocumentChunk(
                document_id=doc.id,
                chunk_index=chunk.chunk_index,
                chunk_text=chunk.text,
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                token_count=chunk.token_count,
                pinecone_id=chunk.chunk_id,
            )
            db.add(db_chunk)

        doc.status = DocumentStatus.INDEXED
        from datetime import datetime
        doc.processed_at = datetime.utcnow()

        logger.info(
            "Policy indexed successfully",
            policy_number=policy_number,
            pages=processed.page_count,
            chunks=len(processed.chunks),
        )

    except Exception as e:
        doc.status = DocumentStatus.FAILED
        doc.error_message = str(e)
        logger.error("Policy processing failed", error=str(e), policy_number=policy_number)
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

    return PolicyUploadResponse(
        job_id=job_id,
        status=doc.status.value,
        policy_number=policy_number,
    )


@router.get("/upload/{job_id}", response_model=PolicyStatusResponse)
async def get_upload_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Check the processing status of a policy upload job."""
    result = await db.execute(
        select(Document).where(
            Document.job_id == job_id,
            Document.tenant_id == tenant_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")

    return PolicyStatusResponse(
        job_id=job_id,
        status=doc.status.value,
        policy_number=doc.policy_number,
        page_count=doc.page_count,
        chunk_count=doc.chunk_count,
        error=doc.error_message,
    )


@router.delete("/{policy_number}", response_model=PolicyDeleteResponse)
async def delete_policy(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Delete a policy and all associated data (S3, Pinecone, DB)."""
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

    # Delete from Pinecone
    from app.models.database import DocumentChunk
    chunks_result = await db.execute(
        select(DocumentChunk.pinecone_id).where(DocumentChunk.document_id == doc.id)
    )
    chunk_ids = [row[0] for row in chunks_result.all() if row[0]]
    if chunk_ids:
        await get_retrieval_service().delete_document_vectors(tenant_id, chunk_ids)

    # Delete from S3
    await get_storage().delete_policy(tenant_id, policy_number)

    # Delete from DB (cascades to chunks)
    await db.delete(doc)

    logger.info("Policy deleted", policy_number=policy_number, tenant_id=tenant_id)

    return PolicyDeleteResponse(
        policy_number=policy_number,
        deleted=True,
        message="Policy and all associated data deleted successfully",
    )


@router.get("/{policy_number}/available", response_model=PolicyAvailableResponse)
async def check_policy_available(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Check if a policy is indexed and available for querying."""
    # Policyholders can only check their own policy
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