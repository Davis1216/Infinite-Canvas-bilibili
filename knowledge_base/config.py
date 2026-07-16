import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JINNI_KB_DATA_DIR", ROOT_DIR / "data" / "knowledge_base"))
FILES_DIR = DATA_DIR / "files"
DATABASE_PATH = Path(os.getenv("JINNI_KB_DATABASE", DATA_DIR / "knowledge_base.sqlite3"))
VECTOR_DIR = Path(os.getenv("JINNI_KB_VECTOR_DIR", DATA_DIR / "vector_store"))


@dataclass(frozen=True)
class KnowledgeSettings:
    database_path: Path = DATABASE_PATH
    files_dir: Path = FILES_DIR
    vector_dir: Path = VECTOR_DIR
    chunk_chars: int = int(os.getenv("JINNI_KB_CHUNK_CHARS", "1800"))
    chunk_overlap: int = int(os.getenv("JINNI_KB_CHUNK_OVERLAP", "220"))
    context_chars: int = int(os.getenv("JINNI_KB_CONTEXT_CHARS", "24000"))
    vector_limit: int = int(os.getenv("JINNI_KB_VECTOR_LIMIT", "24"))
    keyword_limit: int = int(os.getenv("JINNI_KB_KEYWORD_LIMIT", "24"))
    result_limit: int = int(os.getenv("JINNI_KB_RESULT_LIMIT", "10"))
    worker_poll_seconds: float = float(os.getenv("JINNI_KB_WORKER_POLL", "0.8"))
    vector_index_threshold: int = int(os.getenv("JINNI_KB_VECTOR_INDEX_THRESHOLD", "1024"))
    max_upload_bytes: int = int(os.getenv("JINNI_KB_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))


settings = KnowledgeSettings()
