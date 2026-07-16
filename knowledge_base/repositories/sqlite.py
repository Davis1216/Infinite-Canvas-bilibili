import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import settings


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


class KnowledgeRepository:
    def __init__(self, database_path: Path = settings.database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as db:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS embedding_profiles (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL, batch_size INTEGER NOT NULL DEFAULT 32,
                    timeout_seconds REAL NOT NULL DEFAULT 60, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_embedding_profiles_user ON embedding_profiles(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    embedding_profile_id TEXT NOT NULL DEFAULT '', active_generation INTEGER NOT NULL DEFAULT 1,
                    vector_backend TEXT NOT NULL DEFAULT 'lancedb', status TEXT NOT NULL DEFAULT 'ready',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kb_user ON knowledge_bases(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    title TEXT NOT NULL, source_type TEXT NOT NULL, current_version_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_documents_kb ON documents(user_id, knowledge_base_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS document_versions (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, knowledge_base_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL, original_name TEXT NOT NULL, mime_type TEXT NOT NULL DEFAULT '',
                    content_path TEXT NOT NULL, checksum TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id, version_number DESC);

                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL, user_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL, text TEXT NOT NULL, section TEXT NOT NULL DEFAULT '', page INTEGER,
                    vector_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(user_id, knowledge_base_id, generation, ordinal);
                CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(version_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                    chunk_id UNINDEXED, user_id UNINDEXED, knowledge_base_id UNINDEXED,
                    generation UNINDEXED, title, section, text, tokenize='unicode61'
                );

                CREATE TABLE IF NOT EXISTS knowledge_jobs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
                    document_id TEXT NOT NULL DEFAULT '', version_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_queue ON knowledge_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_user ON knowledge_jobs(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS maintenance_suggestions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}', proposed_action_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_user ON maintenance_suggestions(user_id, knowledge_base_id, status);

                CREATE TABLE IF NOT EXISTS jinni_knowledge_bindings (
                    jinni_id TEXT NOT NULL, user_id TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
                    capability TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(jinni_id, knowledge_base_id, capability)
                );
                """)
                profile_columns = {row[1] for row in db.execute("PRAGMA table_info(embedding_profiles)")}
                if "batch_size" not in profile_columns:
                    db.execute("ALTER TABLE embedding_profiles ADD COLUMN batch_size INTEGER NOT NULL DEFAULT 32")
                base_columns = {row[1] for row in db.execute("PRAGMA table_info(knowledge_bases)")}
                if "vector_backend" not in base_columns:
                    db.execute("ALTER TABLE knowledge_bases ADD COLUMN vector_backend TEXT NOT NULL DEFAULT 'lancedb'")
                db.execute("UPDATE knowledge_bases SET vector_backend='lancedb' WHERE vector_backend IS NULL OR vector_backend!='lancedb'")
            self._initialized = True

    @staticmethod
    def _dict(row) -> Optional[Dict[str, Any]]:
        return dict(row) if row else None

    def create_knowledge_base(self, user_id: str, name: str, description: str = "", embedding_profile_id: str = ""):
        record = {"id": new_id(), "user_id": user_id, "name": name.strip()[:120],
                  "description": description.strip()[:1000], "embedding_profile_id": embedding_profile_id,
                  "active_generation": 1, "vector_backend": "lancedb", "status": "ready",
                  "created_at": now_ms(), "updated_at": now_ms()}
        with self.transaction() as db:
            if embedding_profile_id:
                owner = db.execute("SELECT 1 FROM embedding_profiles WHERE id=? AND user_id=?", (embedding_profile_id, user_id)).fetchone()
                if not owner:
                    raise ValueError("嵌入配置不存在")
            db.execute("""INSERT INTO knowledge_bases
                (id,user_id,name,description,embedding_profile_id,active_generation,vector_backend,status,created_at,updated_at)
                VALUES(:id,:user_id,:name,:description,:embedding_profile_id,:active_generation,:vector_backend,:status,:created_at,:updated_at)""", record)
        return record

    def list_knowledge_bases(self, user_id: str, query: str = ""):
        needle = f"%{query.strip()}%"
        with self.connect() as db:
            rows = db.execute("""SELECT kb.*,
                (SELECT COUNT(*) FROM documents d WHERE d.knowledge_base_id=kb.id) AS document_count,
                (SELECT COUNT(*) FROM chunks c WHERE c.knowledge_base_id=kb.id AND c.generation=kb.active_generation) AS chunk_count
                FROM knowledge_bases kb WHERE kb.user_id=? AND (?='' OR kb.name LIKE ? OR kb.description LIKE ?)
                ORDER BY kb.updated_at DESC""", (user_id, query.strip(), needle, needle)).fetchall()
        return [dict(row) for row in rows]

    def get_knowledge_base(self, user_id: str, knowledge_base_id: str):
        with self.connect() as db:
            row = db.execute("""SELECT kb.*,
                (SELECT COUNT(*) FROM documents d WHERE d.knowledge_base_id=kb.id) AS document_count,
                (SELECT COUNT(*) FROM chunks c WHERE c.knowledge_base_id=kb.id AND c.generation=kb.active_generation) AS chunk_count
                FROM knowledge_bases kb WHERE kb.id=? AND kb.user_id=?""", (knowledge_base_id, user_id)).fetchone()
        return self._dict(row)

    def update_knowledge_base(self, user_id: str, knowledge_base_id: str, changes: Dict[str, Any]):
        current = self.get_knowledge_base(user_id, knowledge_base_id)
        if not current:
            return None
        allowed = {key: value for key, value in changes.items() if key in {"name", "description", "embedding_profile_id"} and value is not None}
        if "name" in allowed:
            allowed["name"] = str(allowed["name"]).strip()[:120]
        if "description" in allowed:
            allowed["description"] = str(allowed["description"]).strip()[:1000]
        allowed["updated_at"] = now_ms()
        with self.transaction() as db:
            if allowed.get("embedding_profile_id"):
                owner = db.execute("SELECT 1 FROM embedding_profiles WHERE id=? AND user_id=?", (allowed["embedding_profile_id"], user_id)).fetchone()
                if not owner:
                    raise ValueError("嵌入配置不存在")
            assignments = ",".join(f"{key}=?" for key in allowed)
            db.execute(f"UPDATE knowledge_bases SET {assignments} WHERE id=? AND user_id=?", (*allowed.values(), knowledge_base_id, user_id))
        return self.get_knowledge_base(user_id, knowledge_base_id)

    def delete_knowledge_base(self, user_id: str, knowledge_base_id: str) -> bool:
        with self.transaction() as db:
            chunk_ids = [row[0] for row in db.execute("SELECT id FROM chunks WHERE knowledge_base_id=? AND user_id=?", (knowledge_base_id, user_id))]
            for chunk_id in chunk_ids:
                db.execute("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?", (chunk_id,))
            db.execute("DELETE FROM jinni_knowledge_bindings WHERE knowledge_base_id=? AND user_id=?", (knowledge_base_id, user_id))
            cursor = db.execute("DELETE FROM knowledge_bases WHERE id=? AND user_id=?", (knowledge_base_id, user_id))
        return cursor.rowcount > 0

    def create_document(self, user_id: str, knowledge_base_id: str, title: str, source_type: str,
                        original_name: str, mime_type: str, content_path: str, checksum: str):
        timestamp = now_ms()
        document_id, version_id = new_id(), new_id()
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM knowledge_bases WHERE id=? AND user_id=?", (knowledge_base_id, user_id)).fetchone():
                raise ValueError("知识库不存在")
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                       (document_id, knowledge_base_id, user_id, title[:240], source_type, version_id, "queued", timestamp, timestamp))
            db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (version_id, document_id, knowledge_base_id, user_id, 1, original_name[:240], mime_type[:160], content_path, checksum, "queued", "", timestamp))
        return self.get_document(user_id, document_id)

    def get_document(self, user_id: str, document_id: str):
        with self.connect() as db:
            row = db.execute("""SELECT d.*, v.original_name, v.mime_type, v.content_path, v.checksum, v.error,
                (SELECT COUNT(*) FROM chunks c WHERE c.version_id=v.id) AS chunk_count
                FROM documents d LEFT JOIN document_versions v ON v.id=d.current_version_id
                WHERE d.id=? AND d.user_id=?""", (document_id, user_id)).fetchone()
        return self._dict(row)

    def list_documents(self, user_id: str, knowledge_base_id: str):
        with self.connect() as db:
            rows = db.execute("""SELECT d.*, v.original_name, v.mime_type, v.error,
                (SELECT COUNT(*) FROM chunks c WHERE c.version_id=v.id) AS chunk_count
                FROM documents d LEFT JOIN document_versions v ON v.id=d.current_version_id
                WHERE d.user_id=? AND d.knowledge_base_id=? ORDER BY d.updated_at DESC""", (user_id, knowledge_base_id)).fetchall()
        return [dict(row) for row in rows]

    def preview_document_chunks(self, user_id: str, document_id: str, generation: int,
                                cursor: int = 0, limit: int = 100):
        with self.connect() as db:
            rows = db.execute("""SELECT id AS chunk_id, ordinal, text, section, page
                FROM chunks WHERE user_id=? AND document_id=? AND generation=?
                ORDER BY ordinal LIMIT ? OFFSET ?""",
                (user_id, document_id, int(generation), int(limit) + 1, max(0, int(cursor)))).fetchall()
        return [dict(row) for row in rows]

    def delete_document(self, user_id: str, document_id: str) -> Optional[Dict[str, Any]]:
        document = self.get_document(user_id, document_id)
        if not document:
            return None
        with self.transaction() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM chunks WHERE document_id=? AND user_id=?", (document_id, user_id))]
            for chunk_id in ids:
                db.execute("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?", (chunk_id,))
            db.execute("DELETE FROM documents WHERE id=? AND user_id=?", (document_id, user_id))
        return document

    def create_job(self, user_id: str, knowledge_base_id: str, kind: str, document_id: str = "", version_id: str = "", payload=None):
        timestamp = now_ms()
        record = {"id": new_id(), "user_id": user_id, "knowledge_base_id": knowledge_base_id,
                  "document_id": document_id, "version_id": version_id, "kind": kind, "status": "queued",
                  "progress": 0, "error": "", "attempts": 0, "cancel_requested": 0,
                  "payload_json": json.dumps(payload or {}, ensure_ascii=False), "created_at": timestamp,
                  "updated_at": timestamp, "started_at": None, "finished_at": None}
        with self.transaction() as db:
            db.execute("""INSERT INTO knowledge_jobs VALUES
                (:id,:user_id,:knowledge_base_id,:document_id,:version_id,:kind,:status,:progress,:error,:attempts,
                 :cancel_requested,:payload_json,:created_at,:updated_at,:started_at,:finished_at)""", record)
        return record

    def list_jobs(self, user_id: str, knowledge_base_id: str = ""):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM knowledge_jobs WHERE user_id=? AND (?='' OR knowledge_base_id=?)
                ORDER BY created_at DESC LIMIT 200""", (user_id, knowledge_base_id, knowledge_base_id)).fetchall()
        return [dict(row) for row in rows]

    def claim_job(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM knowledge_jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE knowledge_jobs SET status='running', attempts=attempts+1, started_at=?, updated_at=? WHERE id=? AND status='queued'",
                       (now_ms(), now_ms(), row["id"]))
            claimed = db.execute("SELECT * FROM knowledge_jobs WHERE id=?", (row["id"],)).fetchone()
        return self._dict(claimed)

    def recover_incomplete_jobs(self):
        with self.transaction() as db:
            db.execute("""UPDATE knowledge_jobs SET status='queued', error=CASE WHEN error='' THEN '应用重启后自动恢复' ELSE error END,
                updated_at=?, started_at=NULL WHERE status='running' AND cancel_requested=0""", (now_ms(),))
            db.execute("""UPDATE knowledge_jobs SET status='cancelled', finished_at=?, updated_at=?
                WHERE status IN ('queued','running') AND cancel_requested=1""", (now_ms(), now_ms()))

    def update_job(self, job_id: str, status: Optional[str] = None, progress: Optional[int] = None, error: Optional[str] = None):
        changes = {"updated_at": now_ms()}
        if status is not None:
            changes["status"] = status
            if status in {"completed", "failed", "cancelled", "waiting_configuration"}:
                changes["finished_at"] = now_ms()
        if progress is not None:
            changes["progress"] = max(0, min(100, int(progress)))
        if error is not None:
            changes["error"] = str(error)[:4000]
        with self.transaction() as db:
            db.execute(f"UPDATE knowledge_jobs SET {','.join(f'{k}=?' for k in changes)} WHERE id=?", (*changes.values(), job_id))

    def retry_job(self, user_id: str, job_id: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute("""UPDATE knowledge_jobs SET status='queued', progress=0, error='', cancel_requested=0,
                updated_at=?, finished_at=NULL WHERE id=? AND user_id=? AND status IN ('failed','cancelled','waiting_configuration')""",
                (now_ms(), job_id, user_id))
        return cursor.rowcount > 0

    def cancel_job(self, user_id: str, job_id: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute("""UPDATE knowledge_jobs SET cancel_requested=1,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END, updated_at=?
                WHERE id=? AND user_id=? AND status IN ('queued','running')""", (now_ms(), job_id, user_id))
        return cursor.rowcount > 0

    def job_cancelled(self, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM knowledge_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def get_version(self, version_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM document_versions WHERE id=?", (version_id,)).fetchone()
        return self._dict(row)

    def replace_chunks(self, version: Dict[str, Any], generation: int, chunks: List[Dict[str, Any]]):
        timestamp = now_ms()
        with self.transaction() as db:
            old_ids = [row[0] for row in db.execute("SELECT id FROM chunks WHERE version_id=? AND generation=?", (version["id"], generation))]
            for chunk_id in old_ids:
                db.execute("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?", (chunk_id,))
            db.execute("DELETE FROM chunks WHERE version_id=? AND generation=?", (version["id"], generation))
            title_row = db.execute("SELECT title FROM documents WHERE id=?", (version["document_id"],)).fetchone()
            title = title_row[0] if title_row else version["original_name"]
            for ordinal, chunk in enumerate(chunks):
                chunk_id = chunk.get("id") or new_id()
                chunk["id"] = chunk_id
                db.execute("""INSERT INTO chunks
                    (id,document_id,version_id,knowledge_base_id,user_id,generation,ordinal,text,section,page,vector_id,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, version["document_id"], version["id"], version["knowledge_base_id"], version["user_id"],
                     generation, ordinal, chunk["text"], chunk.get("section", ""), chunk.get("page"), chunk.get("vector_id", chunk_id), timestamp))
                db.execute("INSERT INTO knowledge_chunks_fts VALUES(?,?,?,?,?,?,?)",
                           (chunk_id, version["user_id"], version["knowledge_base_id"], str(generation), title, chunk.get("section", ""), chunk["text"]))
            db.execute("UPDATE document_versions SET status='ready', error='' WHERE id=?", (version["id"],))
            db.execute("UPDATE documents SET status='ready', updated_at=? WHERE id=?", (timestamp, version["document_id"]))
            db.execute("UPDATE knowledge_bases SET updated_at=? WHERE id=?", (timestamp, version["knowledge_base_id"]))

    def activate_generation(self, user_id: str, knowledge_base_id: str, generation: int) -> bool:
        with self.transaction() as db:
            cursor = db.execute("""UPDATE knowledge_bases SET active_generation=?, status='ready', updated_at=?
                WHERE id=? AND user_id=?""", (int(generation), now_ms(), knowledge_base_id, user_id))
        return cursor.rowcount > 0

    def reindex_group_statuses(self, group_id: str):
        needle = f'%"reindex_group": "{group_id}"%'
        with self.connect() as db:
            rows = db.execute("SELECT status FROM knowledge_jobs WHERE payload_json LIKE ? AND kind='index_document'", (needle,)).fetchall()
        return [row[0] for row in rows]

    def fail_version(self, version_id: str, error: str):
        with self.transaction() as db:
            row = db.execute("SELECT document_id FROM document_versions WHERE id=?", (version_id,)).fetchone()
            db.execute("UPDATE document_versions SET status='failed', error=? WHERE id=?", (error[:4000], version_id))
            if row:
                db.execute("UPDATE documents SET status='failed', updated_at=? WHERE id=?", (now_ms(), row[0]))

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
        return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])

    def keyword_search(self, user_id: str, knowledge_base_ids: List[str], query: str, generations: Dict[str, int], limit: int):
        fts_query = self._fts_query(query)
        if not fts_query or not knowledge_base_ids:
            return []
        placeholders = ",".join("?" for _ in knowledge_base_ids)
        with self.connect() as db:
            rows = db.execute(f"""SELECT f.chunk_id, bm25(knowledge_chunks_fts) AS rank,
                c.*, d.title FROM knowledge_chunks_fts f JOIN chunks c ON c.id=f.chunk_id
                JOIN documents d ON d.id=c.document_id
                WHERE knowledge_chunks_fts MATCH ? AND f.user_id=? AND f.knowledge_base_id IN ({placeholders})
                ORDER BY rank LIMIT ?""", (fts_query, user_id, *knowledge_base_ids, limit * 3)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["generation"] != int(generations.get(item["knowledge_base_id"], item["generation"])):
                continue
            item.update({"score": 1.0 / (1.0 + abs(float(item.pop("rank", 0.0)))), "source": "keyword"})
            result.append(item)
            if len(result) >= limit:
                break
        # unicode61 does not segment Chinese text into words. Keep FTS5 as the
        # fast primary path, then use a narrowly scoped substring fallback for
        # CJK queries so a query such as “发布” can match “发布规范”.
        if len(result) < limit and re.search(r"[\u4e00-\u9fff]", query):
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            if escaped:
                pattern = f"%{escaped}%"
                existing = {item["id"] for item in result}
                with self.connect() as db:
                    for knowledge_base_id in knowledge_base_ids:
                        fallback_rows = db.execute("""SELECT c.*, d.title FROM chunks c
                            JOIN documents d ON d.id=c.document_id
                            WHERE c.user_id=? AND c.knowledge_base_id=? AND c.generation=?
                              AND (c.text LIKE ? ESCAPE '\\' OR c.section LIKE ? ESCAPE '\\' OR d.title LIKE ? ESCAPE '\\')
                            ORDER BY c.ordinal LIMIT ?""",
                            (user_id, knowledge_base_id, int(generations.get(knowledge_base_id, 1)),
                             pattern, pattern, pattern, limit - len(result))).fetchall()
                        for row in fallback_rows:
                            item = dict(row)
                            if item["id"] in existing:
                                continue
                            item.update({"score": 0.25, "source": "keyword_substring"})
                            result.append(item)
                            existing.add(item["id"])
                            if len(result) >= limit:
                                break
                        if len(result) >= limit:
                            break
        return result

    def chunks_by_ids(self, user_id: str, chunk_ids: Iterable[str]):
        chunk_ids = list(dict.fromkeys(chunk_ids))
        if not chunk_ids:
            return []
        with self.connect() as db:
            rows = db.execute(f"""SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE c.user_id=? AND c.id IN ({','.join('?' for _ in chunk_ids)})""", (user_id, *chunk_ids)).fetchall()
        lookup = {row["id"]: dict(row) for row in rows}
        return [lookup[item] for item in chunk_ids if item in lookup]

    def create_embedding_profile(self, user_id: str, data: Dict[str, Any]):
        timestamp = now_ms()
        record = {**data, "id": new_id(), "user_id": user_id, "created_at": timestamp, "updated_at": timestamp}
        with self.transaction() as db:
            available = {row[1] for row in db.execute("PRAGMA table_info(embedding_profiles)")}
            columns = ["id", "user_id", "name", "base_url", "api_key", "model", "dimensions",
                       "batch_size", "timeout_seconds", "created_at", "updated_at"]
            # Old databases keep these NOT NULL columns for a non-destructive migration.
            # They are written as inert empty values and never exposed or used again.
            legacy = {"qdrant_url": "", "qdrant_api_key": "", "collection_prefix": ""}
            for key, value in legacy.items():
                if key in available:
                    record[key] = value
                    columns.append(key)
            placeholders = ",".join(f":{column}" for column in columns)
            db.execute(f"INSERT INTO embedding_profiles ({','.join(columns)}) VALUES({placeholders})", record)
        return self.public_profile(record)

    @staticmethod
    def public_profile(profile):
        if not profile:
            return None
        item = dict(profile)
        item["api_key_configured"] = bool(item.pop("api_key", ""))
        item.pop("qdrant_url", None)
        item.pop("qdrant_api_key", None)
        item.pop("collection_prefix", None)
        return item

    def bases_for_local_vector_migration(self):
        with self.connect() as db:
            rows = db.execute("""SELECT kb.*,
                (SELECT COUNT(*) FROM documents d WHERE d.knowledge_base_id=kb.id) AS document_count,
                (SELECT COUNT(*) FROM chunks c WHERE c.knowledge_base_id=kb.id
                    AND c.generation=kb.active_generation) AS chunk_count
                FROM knowledge_bases kb WHERE kb.embedding_profile_id!=''""").fetchall()
        return [dict(row) for row in rows]

    def has_active_reindex_job(self, knowledge_base_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("""SELECT 1 FROM knowledge_jobs WHERE knowledge_base_id=?
                AND kind IN ('reindex_knowledge_base','index_document','activate_generation')
                AND status IN ('queued','running') LIMIT 1""", (knowledge_base_id,)).fetchone()
        return bool(row)

    def list_embedding_profiles(self, user_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM embedding_profiles WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [self.public_profile(row) for row in rows]

    def get_embedding_profile(self, user_id: str, profile_id: str, private: bool = False):
        with self.connect() as db:
            row = db.execute("SELECT * FROM embedding_profiles WHERE id=? AND user_id=?", (profile_id, user_id)).fetchone()
        return dict(row) if row and private else self.public_profile(row)

    def delete_embedding_profile(self, user_id: str, profile_id: str) -> str:
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM knowledge_bases WHERE user_id=? AND embedding_profile_id=?", (user_id, profile_id)).fetchone():
                return "in_use"
            cursor = db.execute("DELETE FROM embedding_profiles WHERE id=? AND user_id=?", (profile_id, user_id))
        return "deleted" if cursor.rowcount else "missing"

    def create_suggestion(self, user_id: str, knowledge_base_id: str, kind: str, title: str, description: str, evidence=None, action=None):
        timestamp = now_ms()
        record = {"id": new_id(), "user_id": user_id, "knowledge_base_id": knowledge_base_id, "kind": kind,
                  "title": title, "description": description, "evidence_json": json.dumps(evidence or {}, ensure_ascii=False),
                  "proposed_action_json": json.dumps(action or {}, ensure_ascii=False), "status": "pending",
                  "created_at": timestamp, "updated_at": timestamp}
        with self.transaction() as db:
            db.execute("INSERT INTO maintenance_suggestions VALUES(:id,:user_id,:knowledge_base_id,:kind,:title,:description,:evidence_json,:proposed_action_json,:status,:created_at,:updated_at)", record)
        return record

    def list_suggestions(self, user_id: str, knowledge_base_id: str = ""):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM maintenance_suggestions WHERE user_id=? AND (?='' OR knowledge_base_id=?)
                ORDER BY created_at DESC""", (user_id, knowledge_base_id, knowledge_base_id)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            item["proposed_action"] = json.loads(item.pop("proposed_action_json") or "{}")
            result.append(item)
        return result

    def decide_suggestion(self, user_id: str, suggestion_id: str, status: str):
        with self.transaction() as db:
            cursor = db.execute("UPDATE maintenance_suggestions SET status=?, updated_at=? WHERE id=? AND user_id=? AND status='pending'",
                                (status, now_ms(), suggestion_id, user_id))
        return cursor.rowcount > 0

    def duplicate_groups(self, user_id: str, knowledge_base_id: str):
        with self.connect() as db:
            rows = db.execute("""SELECT checksum, COUNT(*) AS count, GROUP_CONCAT(document_id) AS document_ids,
                GROUP_CONCAT(original_name, ' | ') AS names FROM document_versions
                WHERE user_id=? AND knowledge_base_id=? GROUP BY checksum HAVING COUNT(*) > 1""",
                (user_id, knowledge_base_id)).fetchall()
        return [dict(row) for row in rows]

    def replace_jinni_bindings(self, user_id: str, jinni_id: str, knowledge_config: Dict[str, Any]):
        with self.transaction() as db:
            db.execute("DELETE FROM jinni_knowledge_bindings WHERE user_id=? AND jinni_id=?", (user_id, jinni_id))
            scopes = {
                "augment": list((knowledge_config.get("augment") or {}).get("knowledge_base_ids") or []),
                "strict": [(knowledge_config.get("strict") or {}).get("knowledge_base_id") or ""],
                "maintain": list((knowledge_config.get("maintain") or {}).get("knowledge_base_ids") or []),
            }
            for capability, knowledge_base_ids in scopes.items():
                if not (knowledge_config.get(capability) or {}).get("enabled"):
                    continue
                for knowledge_base_id in knowledge_base_ids:
                    if not knowledge_base_id or not db.execute(
                        "SELECT 1 FROM knowledge_bases WHERE id=? AND user_id=?", (knowledge_base_id, user_id)
                    ).fetchone():
                        continue
                    db.execute("INSERT OR IGNORE INTO jinni_knowledge_bindings VALUES(?,?,?,?,?)",
                               (jinni_id, user_id, knowledge_base_id, capability, now_ms()))

    def delete_jinni_bindings(self, user_id: str, jinni_id: str):
        with self.transaction() as db:
            db.execute("DELETE FROM jinni_knowledge_bindings WHERE user_id=? AND jinni_id=?", (user_id, jinni_id))


_repository = None
_repository_lock = threading.Lock()


def get_repository() -> KnowledgeRepository:
    global _repository
    with _repository_lock:
        if _repository is None:
            _repository = KnowledgeRepository()
    return _repository
