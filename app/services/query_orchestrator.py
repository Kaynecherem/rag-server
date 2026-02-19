"""Query Orchestrator - RAG pipeline with hallucination prevention."""

import time
import uuid

import structlog

from app.config import get_settings
from app.services.embedding_service import EmbeddingService
from app.services.retrieval_service import RetrievalService, RetrievedChunk
from app.utils.retry import retry_async

logger = structlog.get_logger()
settings = get_settings()


# ── System Prompts ─────────────────────────────────────────────────────────

POLICY_SYSTEM_PROMPT = """You are an insurance policy assistant. Your role is to answer questions 
about insurance policies using ONLY the provided policy excerpts.

STRICT RULES:
1. Answer ONLY using information found in the provided excerpts below.
2. For EVERY claim or fact in your answer, cite the source using [Page X, Section: Y] format.
3. If the answer is not found in the provided excerpts, respond EXACTLY with:
   "I cannot find this information in the policy document."
4. NEVER make up, infer, or assume information not explicitly stated in the excerpts.
5. If information is partial or ambiguous, state what you found and note the limitation.
6. Use clear, plain language that a policyholder can understand.
7. When quoting dollar amounts, limits, or dates, cite the exact excerpt.

You will be provided with numbered excerpts from the policy document. Each excerpt includes 
its page number and section title."""

COMMUNICATION_SYSTEM_PROMPT = """You are an insurance agency document assistant. Your role is to 
answer questions about agency communications, letters, agent notes, and records using ONLY the 
provided document excerpts.

STRICT RULES:
1. Answer ONLY using information found in the provided excerpts below.
2. For EVERY claim or fact in your answer, cite the source using [Page X, Section: Y] format.
3. If the answer is not found in the provided excerpts, respond EXACTLY with:
   "I cannot find this information in the agency records."
4. NEVER make up, infer, or assume information not explicitly stated in the excerpts.
5. Maintain accuracy and professionalism at all times."""


class QueryOrchestrator:
    """
    Orchestrates the full RAG pipeline:
    1. Embed the question
    2. Retrieve relevant chunks (filtered by tenant + policy/comms)
    3. Construct grounded prompt with context
    4. Generate answer with LLM (Claude or OpenAI)
    5. Extract and validate citations
    6. Return structured response
    """

    def __init__(self):
        self.embedding_service = EmbeddingService()
        self.retrieval_service = RetrievalService()
        self.provider = settings.active_llm_provider

        if self.provider == "anthropic":
            import anthropic
            self.llm_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            logger.info("LLM provider: Anthropic (Claude)")
        else:
            from openai import AsyncOpenAI
            self.llm_client = AsyncOpenAI(api_key=settings.openai_api_key)
            logger.info("LLM provider: OpenAI (GPT-4)")

    async def query_policy(
        self,
        question: str,
        tenant_id: str,
        policy_number: str,
    ) -> dict:
        """Execute RAG pipeline against a specific policy."""
        start_time = time.time()
        query_id = str(uuid.uuid4())

        # Step 1: Embed the question
        question_embedding = await self.embedding_service.embed_text(question)

        # Step 2: Retrieve relevant chunks
        retrieved_chunks = await self.retrieval_service.search_policy(
            query_embedding=question_embedding,
            tenant_id=tenant_id,
            policy_number=policy_number,
        )

        if not retrieved_chunks:
            return self._no_results_response(query_id, start_time)

        # Step 3: Rerank by similarity (top K)
        top_chunks = sorted(retrieved_chunks, key=lambda c: c.similarity_score, reverse=True)
        top_chunks = top_chunks[:settings.top_k_rerank]

        # Step 4: Construct prompt
        context = self._build_context(top_chunks)
        prompt = f"""Based on the following policy excerpts, answer this question:

QUESTION: {question}

POLICY EXCERPTS:
{context}

Remember: Only use information from the excerpts above. Cite every fact with [Page X, Section: Y]."""

        # Step 5: Generate answer
        answer = await self._generate_answer(POLICY_SYSTEM_PROMPT, prompt)

        # Step 6: Build response
        latency_ms = int((time.time() - start_time) * 1000)
        confidence = self._calculate_confidence(top_chunks)

        return {
            "answer": answer,
            "citations": self._extract_citations(top_chunks),
            "confidence": confidence,
            "query_id": query_id,
            "latency_ms": latency_ms,
            "retrieval_scores": [
                {"chunk_id": c.chunk_id, "score": round(c.similarity_score, 4)}
                for c in top_chunks
            ],
        }

    async def query_communications(
        self,
        question: str,
        tenant_id: str,
        communication_type: str | None = None,
    ) -> dict:
        """Execute RAG pipeline against agency communications."""
        start_time = time.time()
        query_id = str(uuid.uuid4())

        question_embedding = await self.embedding_service.embed_text(question)

        retrieved_chunks = await self.retrieval_service.search_communications(
            query_embedding=question_embedding,
            tenant_id=tenant_id,
            communication_type=communication_type,
        )

        if not retrieved_chunks:
            return self._no_results_response(query_id, start_time)

        top_chunks = sorted(retrieved_chunks, key=lambda c: c.similarity_score, reverse=True)
        top_chunks = top_chunks[:settings.top_k_rerank]

        context = self._build_context(top_chunks)
        prompt = f"""Based on the following agency document excerpts, answer this question:

QUESTION: {question}

DOCUMENT EXCERPTS:
{context}

Remember: Only use information from the excerpts above. Cite every fact with [Page X, Section: Y]."""

        answer = await self._generate_answer(COMMUNICATION_SYSTEM_PROMPT, prompt)

        latency_ms = int((time.time() - start_time) * 1000)
        confidence = self._calculate_confidence(top_chunks)

        return {
            "answer": answer,
            "citations": self._extract_citations(top_chunks),
            "confidence": confidence,
            "query_id": query_id,
            "latency_ms": latency_ms,
        }

    def _build_context(self, chunks: list[RetrievedChunk]) -> str:
        """Build context string from retrieved chunks with metadata tags."""
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            page = f"Page {chunk.page_number}" if chunk.page_number else "Page unknown"
            section = chunk.section_title or "General"
            context_parts.append(
                f"[Excerpt {i}] [{page}, Section: {section}]\n{chunk.text}\n"
            )
        return "\n---\n".join(context_parts)

    async def _generate_answer(self, system_prompt: str, user_prompt: str) -> str:
        """Generate answer using the configured LLM provider."""
        try:
            if self.provider == "anthropic":
                return await self._generate_anthropic(system_prompt, user_prompt)
            else:
                return await self._generate_openai(system_prompt, user_prompt)
        except Exception as e:
            logger.error("LLM generation failed", error=str(e), provider=self.provider)
            return "I encountered an error while processing your question. Please try again."

    @retry_async(max_retries=2, base_delay=2.0, max_delay=15.0)
    async def _generate_anthropic(self, system_prompt: str, user_prompt: str) -> str:
        """Generate with Claude."""
        response = await self.llm_client.messages.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text

    @retry_async(max_retries=2, base_delay=2.0, max_delay=15.0)
    async def _generate_openai(self, system_prompt: str, user_prompt: str) -> str:
        """Generate with GPT-4."""
        response = await self.llm_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

    def _calculate_confidence(self, chunks: list[RetrievedChunk]) -> float:
        """Calculate confidence score based on retrieval similarity scores."""
        if not chunks:
            return 0.0
        avg_score = sum(c.similarity_score for c in chunks) / len(chunks)
        top_score = chunks[0].similarity_score if chunks else 0.0
        return round(top_score * 0.6 + avg_score * 0.4, 4)

    def _extract_citations(self, chunks: list[RetrievedChunk]) -> list[dict]:
        """Extract citation info from retrieved chunks."""
        return [
            {
                "page": chunk.page_number,
                "section": chunk.section_title or "General",
                "text": chunk.text[:300],
                "chunk_id": chunk.chunk_id,
                "similarity_score": round(chunk.similarity_score, 4),
            }
            for chunk in chunks
        ]

    def _no_results_response(self, query_id: str, start_time: float) -> dict:
        """Response when no relevant chunks are found."""
        return {
            "answer": "I cannot find information related to your question in the available documents.",
            "citations": [],
            "confidence": 0.0,
            "query_id": query_id,
            "latency_ms": int((time.time() - start_time) * 1000),
        }