from .worker import KnowledgeWorker
from .repositories import get_repository
from .vectorstores import create_vector_store


_worker = KnowledgeWorker()


def start_runtime():
    _queue_local_vector_migrations()
    _worker.start()


def stop_runtime():
    _worker.stop()


def _queue_local_vector_migrations():
    """Rebuild legacy/missing remote indexes without making startup depend on LanceDB."""
    repository = get_repository()
    for knowledge_base in repository.bases_for_local_vector_migration():
        if not knowledge_base.get("document_count") or repository.has_active_reindex_job(knowledge_base["id"]):
            continue
        profile = repository.get_embedding_profile(
            knowledge_base["user_id"], knowledge_base.get("embedding_profile_id") or "", private=True
        )
        if not profile:
            continue
        try:
            local_count = create_vector_store(profile).count({
                "user_id": knowledge_base["user_id"],
                "knowledge_base_id": knowledge_base["id"],
                "generation": int(knowledge_base.get("active_generation") or 1),
            })
        except Exception:
            continue
        if local_count < int(knowledge_base.get("chunk_count") or 0) or (
            local_count == 0 and int(knowledge_base.get("document_count") or 0) > 0
        ):
            repository.create_job(knowledge_base["user_id"], knowledge_base["id"], "reindex_knowledge_base",
                                  payload={"migration": "embedded_lancedb"})
