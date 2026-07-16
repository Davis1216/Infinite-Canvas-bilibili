from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    embedding_profile_id: str = ""


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    embedding_profile_id: Optional[str] = None


class TextEntryCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=2_000_000)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=10, ge=1, le=50)
    generation: Optional[int] = None


class EmbeddingProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(default="", max_length=2000)
    model: str = Field(min_length=1, max_length=240)
    dimensions: int = Field(ge=1, le=65535)
    batch_size: int = Field(default=32, ge=1, le=256)
    timeout_seconds: float = Field(default=60, ge=1, le=600)


class MaintenanceCreate(BaseModel):
    knowledge_base_id: str
    kind: str = Field(default="diagnose", pattern="^(diagnose|reindex)$")


class KnowledgeCitation(BaseModel):
    chunk_id: str
    document_id: str
    version_id: str
    title: str
    section: str = ""
    page: Optional[int] = None
    excerpt: str = ""
    score: float = 0.0
    source: str = ""


class RetrievalContext(BaseModel):
    query: str
    text: str = ""
    citations: List[KnowledgeCitation] = []
    grounded: bool = False
    debug: Dict[str, Any] = {}
