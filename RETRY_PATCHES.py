"""
RETRY PATCHES FOR EXISTING SERVICES

Apply these changes to your existing service files to add retry logic
for all external API calls (OpenAI, Anthropic, Pinecone).

Each patch shows the function to modify and the decorator to add.
"""

# ═══════════════════════════════════════════════════════════════════════
# FILE: app/services/embedding_service.py
# Add retry to the OpenAI embedding call
# ═══════════════════════════════════════════════════════════════════════

# ADD this import at the top:
#   from app.utils.retry import retry_async

# FIND the method that calls OpenAI embeddings (likely named embed or get_embedding)
# ADD the decorator:

"""
@retry_async(max_retries=3, base_delay=1.0)
async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
    # ... existing OpenAI embedding call ...
"""


# ═══════════════════════════════════════════════════════════════════════
# FILE: app/services/retrieval_service.py
# Add retry to Pinecone query calls
# ═══════════════════════════════════════════════════════════════════════

# ADD this import at the top:
#   from app.utils.retry import retry_async

# FIND the method that queries Pinecone (likely named query or search)
# ADD the decorator:

"""
@retry_async(max_retries=2, base_delay=0.5)
async def query_vectors(self, embedding: list[float], ...):
    # ... existing Pinecone query call ...
"""


# ═══════════════════════════════════════════════════════════════════════
# FILE: app/services/query_orchestrator.py
# Add retry to the LLM (Claude/OpenAI) call
# ═══════════════════════════════════════════════════════════════════════

# ADD this import at the top:
#   from app.utils.retry import retry_async

# FIND the method that calls the LLM (likely named generate_answer or call_llm)
# ADD the decorator:

"""
@retry_async(max_retries=2, base_delay=2.0, max_delay=15.0)
async def generate_answer(self, question: str, context: str, ...):
    # ... existing Claude/OpenAI LLM call ...
"""


# ═══════════════════════════════════════════════════════════════════════
# QUICK REFERENCE: Where to add @retry_async
# ═══════════════════════════════════════════════════════════════════════
#
# | Service              | Method              | Retries | Base Delay |
# |----------------------|---------------------|---------|------------|
# | embedding_service    | get_embeddings()    | 3       | 1.0s       |
# | retrieval_service    | query_vectors()     | 2       | 0.5s       |
# | query_orchestrator   | generate_answer()   | 2       | 2.0s       |
# | storage_service      | upload_to_s3()      | 3       | 1.0s       |
# | document_processor   | extract_text()      | 1       | 1.0s       |
