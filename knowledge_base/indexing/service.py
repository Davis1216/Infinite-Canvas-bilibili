import json
import uuid
from typing import Dict, List

from ..chunking import chunk_blocks
from ..config import settings
from ..embeddings import OpenAICompatibleEmbeddingProvider
from ..parsers import parse_document
from ..repositories import get_repository
from ..vectorstores import create_vector_store


class IndexingService:
    def __init__(self, repository=None):
        self.repository = repository or get_repository()

    def process_job(self, job: Dict):
        if job["kind"] == "index_document":
            return self._index_document(job)
        if job["kind"] == "diagnose":
            from ..maintenance import MaintenanceService
            MaintenanceService(self.repository).diagnose(job["user_id"], job["knowledge_base_id"])
            self.repository.update_job(job["id"], "completed", 100, "")
            return
        if job["kind"] == "reindex_knowledge_base":
            documents = self.repository.list_documents(job["user_id"], job["knowledge_base_id"])
            knowledge_base = self.repository.get_knowledge_base(job["user_id"], job["knowledge_base_id"])
            if not knowledge_base:
                self.repository.update_job(job["id"], "cancelled", 0, "知识库已删除")
                return
            generation = int(knowledge_base.get("active_generation") or 1) + 1
            group_id = uuid.uuid4().hex
            for document in documents:
                self.repository.create_job(job["user_id"], job["knowledge_base_id"], "index_document",
                                           document["id"], document["current_version_id"],
                                           {"generation": generation, "reindex_group": group_id})
            self.repository.create_job(job["user_id"], job["knowledge_base_id"], "activate_generation",
                                       payload={"generation": generation, "reindex_group": group_id})
            self.repository.update_job(job["id"], "completed", 100, "")
            return
        if job["kind"] == "activate_generation":
            payload = json.loads(job.get("payload_json") or "{}")
            statuses = self.repository.reindex_group_statuses(payload.get("reindex_group") or "")
            failed = [status for status in statuses if status in {"failed", "cancelled"}]
            unfinished = [status for status in statuses if status in {"queued", "running"}]
            waiting = [status for status in statuses if status == "waiting_configuration"]
            if waiting:
                self.repository.update_job(job["id"], "waiting_configuration", 0,
                                           "请先配置嵌入模型，再重试本次索引重建")
                return
            if failed or unfinished:
                raise RuntimeError("新索引代次存在未完成或失败的文档，未切换当前索引")
            knowledge_base = self.repository.get_knowledge_base(job["user_id"], job["knowledge_base_id"])
            profile = self.repository.get_embedding_profile(
                job["user_id"], (knowledge_base or {}).get("embedding_profile_id") or "", private=True
            )
            if profile:
                try:
                    create_vector_store(profile).optimize()
                except Exception:
                    # Optimization is maintenance work. The fully written generation
                    # remains searchable by exact vector scan when it cannot run.
                    pass
            self.repository.activate_generation(job["user_id"], job["knowledge_base_id"], int(payload["generation"]))
            self.repository.update_job(job["id"], "completed", 100, "")
            return
        raise RuntimeError(f"未知知识库任务：{job['kind']}")

    def _index_document(self, job: Dict):
        version = self.repository.get_version(job["version_id"])
        if not version:
            raise RuntimeError("文档版本不存在")
        knowledge_base = self.repository.get_knowledge_base(job["user_id"], job["knowledge_base_id"])
        if not knowledge_base:
            raise RuntimeError("知识库不存在")
        self.repository.update_job(job["id"], progress=8)
        blocks = parse_document(version["content_path"], version["original_name"])
        chunks = chunk_blocks(blocks, settings.chunk_chars, settings.chunk_overlap)
        if not chunks:
            raise RuntimeError("文档中没有可索引的文本内容")
        if self.repository.job_cancelled(job["id"]):
            self.repository.update_job(job["id"], "cancelled", 0, "用户取消任务")
            return
        job_payload = json.loads(job.get("payload_json") or "{}")
        generation = int(job_payload.get("generation") or knowledge_base.get("active_generation") or 1)
        self.repository.update_job(job["id"], progress=25)

        profile_id = knowledge_base.get("embedding_profile_id") or ""
        for chunk in chunks:
            chunk_id = uuid.uuid4().hex
            chunk.update({"id": chunk_id, "vector_id": chunk_id})
        # SQLite/FTS is the durable fallback and must remain usable even when the
        # optional native vector component or embedding API is temporarily down.
        self.repository.replace_chunks(version, generation, chunks)
        if not profile_id:
            self.repository.update_job(job["id"], "waiting_configuration", 45,
                                       "尚未选择嵌入配置；全文索引已经可用，配置后可重新索引向量")
            return
        profile = self.repository.get_embedding_profile(job["user_id"], profile_id, private=True)
        if not profile:
            raise RuntimeError("知识库绑定的嵌入配置不存在")
        embedder = OpenAICompatibleEmbeddingProvider(profile)
        vector_store = create_vector_store(profile)
        points = []
        batch_size = max(1, min(256, int(profile.get("batch_size") or 32)))
        for start in range(0, len(chunks), batch_size):
            if self.repository.job_cancelled(job["id"]):
                self.repository.update_job(job["id"], "cancelled", 0, "用户取消任务")
                return
            batch = chunks[start:start + batch_size]
            vectors = embedder.embed_documents([item["text"] for item in batch])
            for item, vector in zip(batch, vectors):
                points.append({
                    "id": item["id"],
                    "vector": vector,
                    "payload": {
                        "chunk_id": item["id"],
                        "user_id": job["user_id"],
                        "knowledge_base_id": job["knowledge_base_id"],
                        "document_id": version["document_id"],
                        "version_id": version["id"],
                        "generation": generation,
                    },
                })
            progress = 25 + int(50 * min(len(chunks), start + len(batch)) / len(chunks))
            self.repository.update_job(job["id"], progress=progress)
        vector_store.delete({"user_id": job["user_id"], "version_id": version["id"], "generation": generation})
        for start in range(0, len(points), 64):
            vector_store.upsert(points[start:start + 64])
        self.repository.update_job(job["id"], progress=90)
        self.repository.update_job(job["id"], "completed", 100, "")
