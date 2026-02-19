"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from .env file."""

    # App
    app_name: str = "insurance-rag"
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "change-this-to-a-random-secret-key"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/insurance_rag"
    database_url_sync: str = "postgresql+psycopg://postgres:postgres@localhost:5432/insurance_rag"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # OpenAI
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Anthropic (LLM - primary)
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.0

    # LLM Provider: "anthropic" or "openai" (fallback if no Anthropic key)
    llm_provider: str = ""

    @property
    def active_llm_provider(self) -> str:
        """Auto-detect which LLM to use based on available keys."""
        if self.llm_provider:
            return self.llm_provider
        if self.anthropic_api_key:
            return "anthropic"
        return "openai"

    # Pinecone
    pinecone_api_key: str = ""
    pinecone_index_name: str = "insurance-rag"
    pinecone_environment: str = "us-east-1"

    # AWS S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket_name: str = "insurance-rag-documents"

    # Auth0
    auth0_domain: str = "insurance-rag.us.auth0.com"
    auth0_client_id: str = "XjUjYKAkzT12uJ6vGhekTZtBUKIOxx0b"
    auth0_client_secret: str = ""
    auth0_audience: str = "https://api.insurance-rag.com"
    auth0_algorithms: str = "RS256"

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:8000"

    # RAG Config
    chunk_size: int = 512
    chunk_overlap: int = 128
    top_k_retrieval: int = 10
    top_k_rerank: int = 5

    # Rate Limiting
    rate_limit_queries: str = "60/minute"
    rate_limit_uploads: str = "10/minute"
    rate_limit_widget: str = "30/minute"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.debug:
            return ["*"]  # Allow all origins in development (widget testing)
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def auth0_issuer(self) -> str:
        """Full Auth0 issuer URL."""
        return f"https://{self.auth0_domain}/"

    @property
    def auth0_jwks_url(self) -> str:
        """Auth0 JWKS endpoint for RS256 key verification."""
        return f"https://{self.auth0_domain}/.well-known/jwks.json"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()