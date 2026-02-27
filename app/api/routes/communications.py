"""Communication Bucket routes - upload, list, delete agency communications."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.api.dependencies import require_staff, get_tenant_id
from app.models.database import Document, DocumentType, DocumentStatus, DocumentChunk
from app.models.schemas import (
    CommunicationUploadResponse, CommunicationListResponse, CommunicationListItem,
)
from app.services.storage_service import StorageService
from app.services.document_processor import DocumentProcessor
from app.services.embedding_service import EmbeddingService
from app.services.retrieval_service import RetrievalService

logger = structlog.get_logger()
router = APIRouter()

ALLOWED_TYPES = {"letter", "agent_note", "e_and_o", "memo", "claims", "other"}


def get_storage():
    return StorageService()

def get_processor():
    return DocumentProcessor()

def get_embedding_service():
    return EmbeddingService()

def get_retrieval_service():
    return RetrievalService()


@router.post("/upload", response_model=CommunicationUploadResponse)
async def upload_communication(
    file: UploadFile = File(...),
    communication_type: str = Form(...),
    title: str = Form(None),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Upload a communication document (letter, agent note, E&O record, etc.)."""
    if communication_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid communication_type. Allowed: {', '.join(ALLOWED_TYPES)}"
        )

    if not file.filename.lower().endswith((".pdf", ".docx", ".txt")):
        raise HTTPException(status_code=400, detail="Supported formats: PDF, DOCX, TXT")

    file_bytes = await file.read()
    doc_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    # Create document record
    doc = Document(
        id=doc_id,
        tenant_id=tenant_id,
        document_type=DocumentType.COMMUNICATION,
        status=DocumentStatus.PROCESSING,
        communication_type=communication_type,
        filename=file.filename,
        title=title or file.filename,
        s3_key="",
        file_size_bytes=len(file_bytes),
        job_id=job_id,
    )
    db.add(doc)
    await db.flush()

    try:
        # Upload to S3
        s3_key = await get_storage().upload_communication(
            tenant_id, doc_id, file_bytes, file.filename
        )
        doc.s3_key = s3_key

        # Process based on file type
        fname = file.filename.lower()
        if fname.endswith(".pdf"):
            processed = get_processor().process_pdf(file_bytes, file.filename)
        elif fname.endswith(".docx"):
            processed = get_processor().process_docx(file_bytes, file.filename)
        else:
            processed = get_processor().process_txt(file_bytes, file.filename)

        doc.page_count = processed.page_count
        doc.chunk_count = len(processed.chunks)

        # Embed and store
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
                "communication_type": communication_type,
            }
            for c, emb in zip(processed.chunks, embeddings)
        ]

        await get_retrieval_service().upsert_chunks(
            chunks=chunks_for_pinecone,
            tenant_id=tenant_id,
            document_type="communication",
        )

        # Save chunks in DB
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
        doc.processed_at = datetime.utcnow()

        logger.info("Communication indexed", doc_id=doc_id, type=communication_type)

    except Exception as e:
        doc.status = DocumentStatus.FAILED
        doc.error_message = str(e)
        logger.error("Communication processing failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

    await db.commit()

    return CommunicationUploadResponse(
        doc_id=doc_id,
        job_id=job_id,
        status=doc.status.value,
        communication_type=communication_type,
    )


@router.get("", response_model=CommunicationListResponse)
async def list_communications(
    communication_type: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """List agency communications with optional type filter and search."""
    base_filter = [
        Document.tenant_id == tenant_id,
        Document.document_type == DocumentType.COMMUNICATION,
    ]

    if communication_type:
        base_filter.append(Document.communication_type == communication_type)

    if search:
        base_filter.append(
            Document.title.ilike(f"%{search}%") | Document.filename.ilike(f"%{search}%")
        )

    query = select(Document).where(*base_filter)
    count_query = select(func.count(Document.id)).where(*base_filter)

    total = (await db.execute(count_query)).scalar()

    query = query.order_by(Document.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    docs = result.scalars().all()

    return CommunicationListResponse(
        communications=[
            CommunicationListItem(
                doc_id=str(doc.id),
                filename=doc.filename,
                communication_type=doc.communication_type or "other",
                title=doc.title,
                status=doc.status.value,
                page_count=doc.page_count,
                created_at=doc.created_at,
            )
            for doc in docs
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete("/{doc_id}")
async def delete_communication(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """Delete a communication document and all associated data."""
    result = await db.execute(
        select(Document).where(
            Document.id == doc_id,
            Document.tenant_id == tenant_id,
            Document.document_type == DocumentType.COMMUNICATION,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Communication not found")

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

    # Delete from S3
    try:
        from app.services.storage_service import StorageService
        storage = StorageService()
        await storage.delete_communication(tenant_id, doc_id)
    except Exception as e:
        logger.warning("Failed to delete from storage", error=str(e))

    # Delete DB records
    for chunk in chunks:
        await db.delete(chunk)
    await db.delete(doc)
    await db.commit()

    return {"status": "deleted", "doc_id": doc_id}