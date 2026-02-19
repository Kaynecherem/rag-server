"""Embedding Service - Generate embeddings via OpenAI API."""

from app.utils.retry import retry_async
from openai import AsyncOpenAI
import structlog

from app.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class EmbeddingService:
    """Generates text embeddings using OpenAI's embedding API."""

    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions

    @retry_async(max_retries=3, base_delay=1.0)
    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text string."""
        response = await self.client.embeddings.create(
            model=self.model,
            input=text,
            dimensions=self.dimensions,
        )
        return response.data[0].embedding

    @retry_async(max_retries=3, base_delay=1.0)
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in a batch."""
        if not texts:
            return []

        # OpenAI supports batching up to ~2048 inputs
        # Process in batches of 100 for reliability
        all_embeddings = []
        batch_size = 100

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            logger.info("Generating embeddings batch", batch_num=i // batch_size + 1, size=len(batch))

            response = await self.client.embeddings.create(
                model=self.model,
                input=batch,
                dimensions=self.dimensions,
            )

            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)

        return all_embeddings
