"""Retrieval Service - Vector search against Pinecone with tenant isolation."""

from app.utils.retry import retry_async
from dataclasses import dataclass

import structlog

from app.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


@dataclass
class RetrievedChunk:
    """A chunk retrieved from vector search."""
    chunk_id: str
    text: str
    page_number: int | None
    section_title: str | None
    policy_number: str | None
    document_type: str
    similarity_score: float
    metadata: dict


class RetrievalService:
    """Handles vector search with tenant and policy isolation."""

    def __init__(self):
        self._index = None

    @property
    def index(self):
        """Lazy initialization - only connect to Pinecone when first needed."""
        if self._index is None:
            from pinecone import Pinecone
            pc = Pinecone(api_key=settings.pinecone_api_key)
            self._index = pc.Index(settings.pinecone_index_name)
            logger.info("Connected to Pinecone", index=settings.pinecone_index_name)
        return self._index

    @retry_async(max_retries=2, base_delay=1.0)
    async def upsert_chunks(
        self,
        chunks: list[dict],
        tenant_id: str,
        document_type: str,
        policy_number: str | None = None,
    ):
        """
        Store chunk embeddings in Pinecone with metadata for filtering.
        Uses tenant_id as the Pinecone namespace for isolation.
        """
        vectors = []
        for chunk in chunks:
            metadata = {
                "tenant_id": tenant_id,
                "document_type": document_type,
                "chunk_text": chunk["text"][:1000],  # Pinecone metadata limit
                "page_number": chunk.get("page_number") or 0,
                "section_title": chunk.get("section_title") or "",
                "chunk_index": chunk["chunk_index"],
            }
            if policy_number:
                metadata["policy_number"] = policy_number

            if chunk.get("communication_type"):
                metadata["communication_type"] = chunk["communication_type"]

            vectors.append({
                "id": chunk["chunk_id"],
                "values": chunk["embedding"],
                "metadata": metadata,
            })

        # Upsert in batches of 100 (Pinecone limit)
        batch_size = 100
        namespace = tenant_id
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            self.index.upsert(vectors=batch, namespace=namespace)
            logger.info("Upserted vectors", batch=i // batch_size + 1, count=len(batch), namespace=namespace)

    @retry_async(max_retries=2, base_delay=0.5)
    async def search_policy(
        self,
        query_embedding: list[float],
        tenant_id: str,
        policy_number: str,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Search for chunks within a specific policy. Used by policyholders and staff."""
        top_k = top_k or settings.top_k_retrieval

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=tenant_id,
            filter={
                "document_type": {"$eq": "policy"},
                "policy_number": {"$eq": policy_number},
            },
            include_metadata=True,
        )

        return self._parse_results(results)

    @retry_async(max_retries=2, base_delay=0.5)
    async def search_communications(
        self,
        query_embedding: list[float],
        tenant_id: str,
        top_k: int | None = None,
        communication_type: str | None = None,
    ) -> list[RetrievedChunk]:
        """Search across agency communications. Staff only."""
        top_k = top_k or settings.top_k_retrieval

        filter_dict = {"document_type": {"$eq": "communication"}}
        if communication_type:
            filter_dict["communication_type"] = {"$eq": communication_type}

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=tenant_id,
            filter=filter_dict,
            include_metadata=True,
        )

        return self._parse_results(results)

    async def delete_document_vectors(self, tenant_id: str, chunk_ids: list[str]):
        """Delete all vectors for a specific document."""
        if not chunk_ids:
            return

        # Delete in batches of 1000
        batch_size = 1000
        for i in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[i:i + batch_size]
            self.index.delete(ids=batch, namespace=tenant_id)
            logger.info("Deleted vectors", count=len(batch), namespace=tenant_id)

    def _parse_results(self, results) -> list[RetrievedChunk]:
        """Parse Pinecone query results into RetrievedChunk objects."""
        chunks = []
        for match in results.get("matches", []):
            meta = match.get("metadata", {})
            chunks.append(RetrievedChunk(
                chunk_id=match["id"],
                text=meta.get("chunk_text", ""),
                page_number=meta.get("page_number"),
                section_title=meta.get("section_title"),
                policy_number=meta.get("policy_number"),
                document_type=meta.get("document_type", "policy"),
                similarity_score=match.get("score", 0.0),
                metadata=meta,
            ))
        return chunks