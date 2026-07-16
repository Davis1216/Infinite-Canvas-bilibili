from collections import defaultdict
from typing import Dict, List, Optional

from ..config import settings
from ..domain.schemas import KnowledgeCitation, RetrievalContext
from ..embeddings import OpenAICompatibleEmbeddingProvider
from ..repositories import get_repository
from ..vectorstores import create_vector_store


class RetrievalService:
    def __init__(self, repository=None):
        self.repository = repository or get_repository()

    @staticmethod
    def _rrf(result_lists: List[List[Dict]], limit: int, k: int = 60):
        scores, sources, originals = defaultdict(float), defaultdict(set), {}
        for items in result_lists:
            for rank, item in enumerate(items, 1):
                chunk_id = item.get("chunk_id") or item.get("id")
                if not chunk_id:
                    continue
                scores[chunk_id] += 1.0 / (k + rank)
                sources[chunk_id].add(item.get("source") or "unknown")
                originals[chunk_id] = item
        ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)[:limit]
        return [{**originals[chunk_id], "chunk_id": chunk_id, "score": scores[chunk_id],
                 "source": "+".join(sorted(sources[chunk_id]))} for chunk_id in ordered]

    def retrieve(self, user_id: str, knowledge_base_ids: List[str], query: str,
                 limit: int = settings.result_limit, generations: Optional[Dict[str, int]] = None) -> RetrievalContext:
        bases = []
        for knowledge_base_id in list(dict.fromkeys(knowledge_base_ids)):
            item = self.repository.get_knowledge_base(user_id, knowledge_base_id)
            if item:
                bases.append(item)
        if not bases:
            return RetrievalContext(query=query, grounded=False, debug={"reason": "knowledge_base_not_found"})
        generations = generations or {item["id"]: int(item.get("active_generation") or 1) for item in bases}
        keyword = self.repository.keyword_search(user_id, [item["id"] for item in bases], query, generations, settings.keyword_limit)

        vector_results = []
        groups = defaultdict(list)
        for item in bases:
            if item.get("embedding_profile_id"):
                groups[item["embedding_profile_id"]].append(item)
        vector_errors = []
        for profile_id, group in groups.items():
            profile = self.repository.get_embedding_profile(user_id, profile_id, private=True)
            if not profile:
                continue
            try:
                vector = OpenAICompatibleEmbeddingProvider(profile).embed_query(query)
                store = create_vector_store(profile)
                ids = [item["id"] for item in group]
                # Different generations require separate filtered queries to preserve frozen snapshots.
                for base in group:
                    points = store.search(vector, {
                        "user_id": user_id,
                        "knowledge_base_id": base["id"],
                        "generation": int(generations.get(base["id"], base.get("active_generation") or 1)),
                    }, settings.vector_limit)
                    for point in points:
                        payload = point.get("payload") or {}
                        vector_results.append({"chunk_id": payload.get("chunk_id"), "score": point["score"], "source": "vector"})
            except Exception as exc:
                vector_errors.append(str(exc))
        fused = self._rrf([vector_results, keyword], max(limit * 2, limit))
        rows = self.repository.chunks_by_ids(user_id, [item["chunk_id"] for item in fused])
        by_id = {item["id"]: item for item in rows}
        citations, context_parts, used = [], [], 0
        for fused_item in fused:
            row = by_id.get(fused_item["chunk_id"])
            if not row:
                continue
            text = row["text"]
            remain = settings.context_chars - used
            if remain <= 0:
                break
            excerpt = text[:min(len(text), remain)]
            index = len(citations) + 1
            location = f"第 {row['page']} 页" if row.get("page") else row.get("section") or "正文"
            context_parts.append(f"[{index}] {row['title']} · {location}\n{excerpt}")
            citations.append(KnowledgeCitation(
                chunk_id=row["id"], document_id=row["document_id"], version_id=row["version_id"],
                title=row["title"], section=row.get("section") or "", page=row.get("page"),
                excerpt=excerpt[:500], score=float(fused_item["score"]), source=fused_item["source"],
            ))
            used += len(excerpt)
            if len(citations) >= limit:
                break
        return RetrievalContext(query=query, text="\n\n---\n\n".join(context_parts), citations=citations,
                                grounded=bool(citations), debug={"vector_count": len(vector_results),
                                "keyword_count": len(keyword), "vector_errors": vector_errors})
