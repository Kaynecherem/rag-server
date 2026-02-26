"""Batch upload routes - upload multiple files in a single request."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.db.session import get_db
from app.api.dependencies import require_staff, get_tenant_id
from app.models.database import Document, DocumentType, DocumentStatus, DocumentChunk
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


MAX_POLICY_FILES = 10
MAX_COMM_FILES = 20
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file


# ── Batch Policy Upload ───────────────────────────────────────────────────

@router.post("/policies/upload-batch")
async def upload_policies_batch(
    files: list[UploadFile] = File(..., description="Up to 10 PDF files"),
    policy_numbers: str = Form(..., description="Comma-separated policy numbers, one per file"),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Upload multiple policy PDFs in a single request.

    - files: Up to 10 PDF files
    - policy_numbers: Comma-separated list matching each file (e.g. "POL-001,POL-002,POL-003")

    Returns per-file results with overall summary.
    """
    numbers = [n.strip() for n in policy_numbers.split(",") if n.strip()]

    if len(files) > MAX_POLICY_FILES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_POLICY_FILES} files per batch")

    if len(numbers) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f"Got {len(numbers)} policy numbers but {len(files)} files. "
                   f"Provide one comma-separated policy number per file."
        )

    # Pre-validate all files
    file_data = []
    for i, file in enumerate(files):
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"'{file.filename}' is not a PDF")

        file_bytes = await file.read()
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"'{file.filename}' exceeds 50MB limit")
        if len(file_bytes) == 0:
            raise HTTPException(status_code=400, detail=f"'{file.filename}' is empty")

        file_data.append({
            "filename": file.filename,
            "bytes": file_bytes,
            "policy_number": numbers[i],
        })

    # Process each file
    results = []
    for item in file_data:
        result = await _process_policy(
            db, tenant_id, item["filename"], item["bytes"], item["policy_number"]
        )
        results.append(result)

    succeeded = sum(1 for r in results if r["status"] == "indexed")
    failed = sum(1 for r in results if r["status"] == "failed")

    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


# ── Batch Communication Upload ────────────────────────────────────────────

@router.post("/communications/upload-batch")
async def upload_communications_batch(
    files: list[UploadFile] = File(..., description="Up to 20 files (PDF, DOCX, TXT)"),
    communication_type: str = Form(..., description="Type for all files: letter, agent_note, e_and_o, memo, claims, other"),
    titles: str | None = Form(None, description="Optional comma-separated titles, one per file"),
    db: AsyncSession = Depends(get_db),
    staff: dict = Depends(require_staff),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Upload multiple communication documents in a single request.

    - files: Up to 20 files (PDF, DOCX, TXT)
    - communication_type: Applied to all files
    - titles: Optional comma-separated titles (defaults to filenames)

    Returns per-file results with overall summary.
    """
    ALLOWED_TYPES = {"letter", "agent_note", "e_and_o", "memo", "claims", "other"}
    if communication_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid communication_type. Allowed: {', '.join(ALLOWED_TYPES)}"
        )

    if len(files) > MAX_COMM_FILES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_COMM_FILES} files per batch")

    title_list = []
    if titles:
        title_list = [t.strip() for t in titles.split(",")]
        if len(title_list) != len(files):
            raise HTTPException(
                status_code=400,
                detail=f"Got {len(title_list)} titles but {len(files)} files"
            )

    # Pre-validate all files
    file_data = []
    for i, file in enumerate(files):
        ext = file.filename.lower().rsplit(".", 1)[-1] if "." in file.filename else ""
        if ext not in ("pdf", "docx", "txt"):
            raise HTTPException(status_code=400, detail=f"'{file.filename}' is not a supported format (PDF, DOCX, TXT)")

        file_bytes = await file.read()
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"'{file.filename}' exceeds 50MB limit")
        if len(file_bytes) == 0:
            raise HTTPException(status_code=400, detail=f"'{file.filename}' is empty")

        file_data.append({
            "filename": file.filename,
            "bytes": file_bytes,
            "title": title_list[i] if i < len(title_list) else file.filename,
        })

    # Process each file
    results = []
    for item in file_data:
        result = await _process_communication(
            db, tenant_id, item["filename"], item["bytes"],
            communication_type, item["title"],
        )
        results.append(result)

    succeeded = sum(1 for r in results if r["status"] == "indexed")
    failed = sum(1 for r in results if r["status"] == "failed")

    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


# ── Processing helpers ─────────────────────────────────────────────────────

async def _process_policy(
    db: AsyncSession, tenant_id: str,
    filename: str, file_bytes: bytes, policy_number: str,
) -> dict:
    """Process a single policy PDF — upload, chunk, embed, index."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    doc = Document(
        tenant_id=tenant_id,
        document_type=DocumentType.POLICY,
        status=DocumentStatus.PROCESSING,
        policy_number=policy_number,
        filename=filename,
        s3_key="",
        file_size_bytes=len(file_bytes),
        job_id=job_id,
    )
    db.add(doc)
    await db.flush()

    try:
        s3_key = await get_storage().upload_policy(tenant_id, policy_number, file_bytes, filename)
        doc.s3_key = s3_key

        processed = get_processor().process_pdf(file_bytes, filename)
        doc.page_count = processed.page_count
        doc.chunk_count = len(processed.chunks)

        chunk_texts = [c.text for c in processed.chunks]
        embeddings = await get_embedding_service().embed_texts(chunk_texts)

        chunks_for_pinecone = [
            {
                "chunk_id": c.chunk_id, "text": c.text, "embedding": emb,
                "page_number": c.page_number, "section_title": c.section_title,
                "chunk_index": c.chunk_index,
            }
            for c, emb in zip(processed.chunks, embeddings)
        ]

        await get_retrieval_service().upsert_chunks(
            chunks=chunks_for_pinecone, tenant_id=tenant_id,
            document_type="policy", policy_number=policy_number,
        )

        for chunk in processed.chunks:
            db.add(DocumentChunk(
                document_id=doc.id, chunk_index=chunk.chunk_index,
                chunk_text=chunk.text, page_number=chunk.page_number,
                section_title=chunk.section_title, token_count=chunk.token_count,
                pinecone_id=chunk.chunk_id,
            ))

        doc.status = DocumentStatus.INDEXED
        doc.processed_at = datetime.utcnow()

        logger.info("Policy indexed (batch)", policy_number=policy_number,
                     pages=processed.page_count, chunks=len(processed.chunks))

        return {
            "policy_number": policy_number, "filename": filename,
            "status": "indexed", "job_id": job_id,
            "page_count": processed.page_count,
            "chunk_count": len(processed.chunks), "error": None,
        }

    except Exception as e:
        doc.status = DocumentStatus.FAILED
        doc.error_message = str(e)
        logger.error("Policy batch processing failed", error=str(e), policy_number=policy_number)
        return {
            "policy_number": policy_number, "filename": filename,
            "status": "failed", "job_id": job_id,
            "page_count": None, "chunk_count": None, "error": str(e),
        }


async def _process_communication(
    db: AsyncSession, tenant_id: str,
    filename: str, file_bytes: bytes,
    communication_type: str, title: str,
) -> dict:
    """Process a single communication document."""
    doc_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    doc = Document(
        id=doc_id,
        tenant_id=tenant_id,
        document_type=DocumentType.COMMUNICATION,
        status=DocumentStatus.PROCESSING,
        communication_type=communication_type,
        filename=filename,
        title=title,
        s3_key="",
        file_size_bytes=len(file_bytes),
        job_id=job_id,
    )
    db.add(doc)
    await db.flush()

    try:
        s3_key = await get_storage().upload_communication(
            tenant_id, doc_id, file_bytes, filename
        )
        doc.s3_key = s3_key

        if filename.lower().endswith(".pdf"):
            processed = get_processor().process_pdf(file_bytes, filename)
        else:
            text = file_bytes.decode("utf-8", errors="replace")
            from app.services.document_processor import Chunk, ProcessedDocument
            processed = ProcessedDocument(
                full_text=text,
                chunks=[Chunk(
                    chunk_id=str(uuid.uuid4()), chunk_index=0,
                    text=text, page_number=1, section_title=None,
                    token_count=len(text.split()),
                )],
                page_count=1,
                metadata={"filename": filename},
            )

        doc.page_count = processed.page_count
        doc.chunk_count = len(processed.chunks)

        chunk_texts = [c.text for c in processed.chunks]
        embeddings = await get_embedding_service().embed_texts(chunk_texts)

        chunks_for_pinecone = [
            {
                "chunk_id": c.chunk_id, "text": c.text, "embedding": emb,
                "page_number": c.page_number, "section_title": c.section_title,
                "chunk_index": c.chunk_index,
                "communication_type": communication_type,
            }
            for c, emb in zip(processed.chunks, embeddings)
        ]

        await get_retrieval_service().upsert_chunks(
            chunks=chunks_for_pinecone, tenant_id=tenant_id,
            document_type="communication",
        )

        for chunk in processed.chunks:
            db.add(DocumentChunk(
                document_id=doc.id, chunk_index=chunk.chunk_index,
                chunk_text=chunk.text, page_number=chunk.page_number,
                section_title=chunk.section_title, token_count=chunk.token_count,
                pinecone_id=chunk.chunk_id,
            ))

        doc.status = DocumentStatus.INDEXED
        doc.processed_at = datetime.utcnow()

        logger.info("Communication indexed (batch)", doc_id=doc_id, type=communication_type)

        return {
            "doc_id": doc_id, "filename": filename,
            "status": "indexed", "job_id": job_id,
            "communication_type": communication_type,
            "page_count": processed.page_count,
            "chunk_count": len(processed.chunks), "error": None,
        }

    except Exception as e:
        doc.status = DocumentStatus.FAILED
        doc.error_message = str(e)
        logger.error("Communication batch processing failed", error=str(e))
        return {
            "doc_id": doc_id, "filename": filename,
            "status": "failed", "job_id": job_id,
            "communication_type": communication_type,
            "page_count": None, "chunk_count": None, "error": str(e),
        }