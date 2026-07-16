from ..repositories import get_repository


class MaintenanceService:
    def __init__(self, repository=None):
        self.repository = repository or get_repository()

    def diagnose(self, user_id: str, knowledge_base_id: str):
        created = []
        for group in self.repository.duplicate_groups(user_id, knowledge_base_id):
            created.append(self.repository.create_suggestion(
                user_id, knowledge_base_id, "duplicate", "发现完全重复的文档",
                f"以下文档内容校验值相同：{group.get('names') or ''}",
                {"checksum": group["checksum"], "document_ids": (group.get("document_ids") or "").split(",")},
                {"action": "review_duplicates"},
            ))
        for document in self.repository.list_documents(user_id, knowledge_base_id):
            if document.get("status") == "failed":
                created.append(self.repository.create_suggestion(
                    user_id, knowledge_base_id, "index_error", f"索引失败：{document['title']}",
                    document.get("error") or "该文档索引失败，请检查文件或重新执行。",
                    {"document_id": document["id"]}, {"action": "retry_index", "document_id": document["id"]},
                ))
        if not created:
            created.append(self.repository.create_suggestion(
                user_id, knowledge_base_id, "health", "知识库检查完成",
                "暂未发现完全重复文档或失败索引。后续接入语义模型后可扩展近似重复和冲突检测。",
                {}, {"action": "none"},
            ))
        return created
