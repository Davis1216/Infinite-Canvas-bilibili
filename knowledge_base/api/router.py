import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile

from ..config import settings
from ..domain.schemas import (EmbeddingProfileCreate, KnowledgeBaseCreate, KnowledgeBaseUpdate,
                              MaintenanceCreate, SearchRequest, TextEntryCreate)
from ..parsers.documents import SUPPORTED_EXTENSIONS, TEXT_EXTENSIONS, parse_document
from ..repositories import get_repository
from ..retrieval import RetrievalService
from ..vectorstores import create_vector_store


router = APIRouter(tags=["Jinni Knowledge Base"])


def safe_user_id(value: str, request: Request) -> str:
    candidate = (value or "").strip()
    if not candidate and request.client:
        candidate = f"ip-{request.client.host}"
    candidate = re.sub(r"[^a-zA-Z0-9_.-]", "-", candidate or "anonymous")[:80].strip(".-")
    return candidate or "anonymous"


def require_base(repository, user_id: str, knowledge_base_id: str):
    item = repository.get_knowledge_base(user_id, knowledge_base_id)
    if not item:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return item


@router.get("/api/knowledge-bases")
async def list_knowledge_bases(request: Request, q: str = "", x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"knowledge_bases": get_repository().list_knowledge_bases(user_id, q)}


@router.post("/api/knowledge-bases")
async def create_knowledge_base(payload: KnowledgeBaseCreate, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    try:
        item = get_repository().create_knowledge_base(user_id, payload.name, payload.description, payload.embedding_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"knowledge_base": item}


@router.get("/api/knowledge-bases/{knowledge_base_id}")
async def get_knowledge_base(knowledge_base_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    item = require_base(repository, user_id, knowledge_base_id)
    return {"knowledge_base": item, "documents": repository.list_documents(user_id, knowledge_base_id)}


@router.patch("/api/knowledge-bases/{knowledge_base_id}")
async def update_knowledge_base(knowledge_base_id: str, payload: KnowledgeBaseUpdate, request: Request,
                                x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    try:
        item = get_repository().update_knowledge_base(user_id, knowledge_base_id, payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not item:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"knowledge_base": item}


@router.delete("/api/knowledge-bases/{knowledge_base_id}")
async def delete_knowledge_base(knowledge_base_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    item = require_base(repository, user_id, knowledge_base_id)
    profile = repository.get_embedding_profile(user_id, item.get("embedding_profile_id") or "", private=True)
    if profile:
        try:
            create_vector_store(profile).delete({"user_id": user_id, "knowledge_base_id": knowledge_base_id})
        except Exception:
            pass
    deleted = repository.delete_knowledge_base(user_id, knowledge_base_id)
    shutil.rmtree(settings.files_dir / user_id / knowledge_base_id, ignore_errors=True)
    return {"ok": deleted}


def _store_bytes(user_id: str, knowledge_base_id: str, filename: str, content: bytes):
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"{filename} 格式不受支持")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"{filename} 超过 50MB 上限")
    target_dir = settings.files_dir / user_id / knowledge_base_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{extension}"
    target.write_bytes(content)
    return target, hashlib.sha256(content).hexdigest()


@router.get("/api/knowledge-bases/{knowledge_base_id}/documents")
async def list_documents(knowledge_base_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    require_base(repository, user_id, knowledge_base_id)
    return {"documents": repository.list_documents(user_id, knowledge_base_id)}


@router.post("/api/knowledge-bases/{knowledge_base_id}/documents")
async def upload_documents(knowledge_base_id: str, request: Request, files: List[UploadFile] = File(...),
                           x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    require_base(repository, user_id, knowledge_base_id)
    created = []
    for upload in files:
        content = await upload.read()
        target, checksum = _store_bytes(user_id, knowledge_base_id, upload.filename or "document.txt", content)
        try:
            document = repository.create_document(user_id, knowledge_base_id, upload.filename or target.name, "file",
                                                  upload.filename or target.name, upload.content_type or "", str(target), checksum)
            job = repository.create_job(user_id, knowledge_base_id, "index_document", document["id"], document["current_version_id"])
            created.append({"document": document, "job": job})
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return {"items": created}


@router.get("/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/preview")
async def preview_document(knowledge_base_id: str, document_id: str, request: Request, view: str = "read",
                           cursor: int = 0, limit: int = 100, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    knowledge_base = require_base(repository, user_id, knowledge_base_id)
    document = repository.get_document(user_id, document_id)
    if not document or document.get("knowledge_base_id") != knowledge_base_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    if view not in {"read", "chunks", "raw"}:
        raise HTTPException(status_code=400, detail="不支持的预览视图")
    cursor = max(0, int(cursor or 0))
    limit = max(1, min(200, int(limit or 100)))
    extension = Path(document.get("original_name") or "").suffix.lower()
    public_document = {key: document.get(key) for key in (
        "id", "title", "source_type", "original_name", "mime_type", "status", "error", "chunk_count"
    )}

    if view == "chunks":
        rows = repository.preview_document_chunks(
            user_id, document_id, int(knowledge_base.get("active_generation") or 1), cursor, limit
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {"document": public_document, "format": extension.lstrip(".") or "text", "view": view,
                "blocks": rows, "next_cursor": cursor + len(rows) if has_more else None,
                "truncated": has_more}

    path = Path(document.get("content_path") or "")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文档源文件已丢失，无法预览")
    try:
        if view == "raw" and extension in TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8", errors="replace")
            parsed = [{"text": text, "section": "", "page": None, "kind": "source"}]
        else:
            parsed = [
                {"text": item.text, "section": item.section, "page": item.page, "kind": item.kind}
                for item in parse_document(str(path), document.get("original_name") or path.name)
            ]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"文档解析失败：{exc}") from exc

    # Split unusually large paragraphs/source files into cursor-addressable
    # blocks so "continue loading" never loses the tail of a document.
    pageable = []
    for item in parsed:
        text = str(item.get("text") or "")
        if not text:
            continue
        for start in range(0, len(text), 100_000):
            part = dict(item)
            part["text"] = text[start:start + 100_000]
            pageable.append(part)
    parsed = pageable

    char_budget = 200_000
    selected, used, index = [], 0, cursor
    while index < len(parsed) and len(selected) < limit:
        item = dict(parsed[index])
        text = str(item.get("text") or "")
        remaining = char_budget - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            item["text"] = text[:remaining]
            item["content_truncated"] = True
        selected.append(item)
        used += len(item["text"])
        index += 1
        if item.get("content_truncated"):
            break
    has_more = index < len(parsed)
    return {"document": public_document, "format": extension.lstrip(".") or "text", "view": view,
            "blocks": selected, "next_cursor": index if has_more else None, "truncated": has_more}


@router.post("/api/knowledge-bases/{knowledge_base_id}/entries")
async def create_text_entry(knowledge_base_id: str, payload: TextEntryCreate, request: Request,
                            x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    require_base(repository, user_id, knowledge_base_id)
    content = payload.content.encode("utf-8")
    target, checksum = _store_bytes(user_id, knowledge_base_id, f"{payload.title}.md", content)
    document = repository.create_document(user_id, knowledge_base_id, payload.title, "text", f"{payload.title}.md",
                                          "text/markdown", str(target), checksum)
    job = repository.create_job(user_id, knowledge_base_id, "index_document", document["id"], document["current_version_id"])
    return {"document": document, "job": job}


@router.delete("/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}")
async def delete_document(knowledge_base_id: str, document_id: str, request: Request,
                          x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    knowledge_base = require_base(repository, user_id, knowledge_base_id)
    document = repository.get_document(user_id, document_id)
    if not document or document["knowledge_base_id"] != knowledge_base_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    profile = repository.get_embedding_profile(user_id, knowledge_base.get("embedding_profile_id") or "", private=True)
    if profile:
        try:
            create_vector_store(profile).delete({"user_id": user_id, "document_id": document_id})
        except Exception:
            pass
    repository.delete_document(user_id, document_id)
    path = document.get("content_path")
    if path:
        Path(path).unlink(missing_ok=True)
    return {"ok": True}


@router.post("/api/knowledge-bases/{knowledge_base_id}/search")
async def search_knowledge_base(knowledge_base_id: str, payload: SearchRequest, request: Request,
                                x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    knowledge_base = require_base(repository, user_id, knowledge_base_id)
    generations = {knowledge_base_id: payload.generation or int(knowledge_base.get("active_generation") or 1)}
    result = RetrievalService(repository).retrieve(user_id, [knowledge_base_id], payload.query, payload.limit, generations)
    return {"result": result.model_dump() if hasattr(result, "model_dump") else result.dict()}


@router.post("/api/knowledge-bases/{knowledge_base_id}/reindex")
async def reindex_knowledge_base(knowledge_base_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    require_base(repository, user_id, knowledge_base_id)
    return {"job": repository.create_job(user_id, knowledge_base_id, "reindex_knowledge_base")}


@router.get("/api/knowledge-jobs")
async def list_jobs(request: Request, knowledge_base_id: str = "", x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"jobs": get_repository().list_jobs(user_id, knowledge_base_id)}


@router.post("/api/knowledge-jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    if not get_repository().retry_job(user_id, job_id):
        raise HTTPException(status_code=404, detail="任务不存在或当前状态不可重试")
    return {"ok": True}


@router.post("/api/knowledge-jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    if not get_repository().cancel_job(user_id, job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已经结束")
    return {"ok": True}


@router.get("/api/embedding-profiles")
async def list_embedding_profiles(request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"profiles": get_repository().list_embedding_profiles(user_id)}


@router.post("/api/embedding-profiles")
async def create_embedding_profile(payload: EmbeddingProfileCreate, request: Request,
                                   x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    return {"profile": get_repository().create_embedding_profile(user_id, data)}


@router.delete("/api/embedding-profiles/{profile_id}")
async def delete_embedding_profile(profile_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    result = get_repository().delete_embedding_profile(user_id, profile_id)
    if result == "in_use":
        raise HTTPException(status_code=409, detail="该嵌入配置仍被知识库使用")
    if result == "missing":
        raise HTTPException(status_code=404, detail="嵌入配置不存在")
    return {"ok": True}


@router.get("/api/vector-store/health")
async def vector_store_health(request: Request, profile_id: str = "", x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    profiles = [repository.get_embedding_profile(user_id, profile_id, private=True)] if profile_id else []
    if not profiles:
        public = repository.list_embedding_profiles(user_id)
        profiles = [repository.get_embedding_profile(user_id, item["id"], private=True) for item in public]
    profile_health = [{"id": item["id"], **create_vector_store(item).health()} for item in profiles if item]
    path = settings.vector_dir.resolve()
    size_bytes = 0
    if path.exists():
        try:
            size_bytes = sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
        except OSError:
            pass
    return {"backend": "lancedb", "embedded": True, "path": str(path),
            "size_bytes": size_bytes, "profiles": profile_health}


@router.get("/api/knowledge-maintenance")
async def list_maintenance(request: Request, knowledge_base_id: str = "", x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"suggestions": get_repository().list_suggestions(user_id, knowledge_base_id)}


@router.post("/api/knowledge-maintenance")
async def create_maintenance(payload: MaintenanceCreate, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    repository = get_repository()
    require_base(repository, user_id, payload.knowledge_base_id)
    kind = "diagnose" if payload.kind == "diagnose" else "reindex_knowledge_base"
    return {"job": repository.create_job(user_id, payload.knowledge_base_id, kind)}


@router.post("/api/knowledge-maintenance/{suggestion_id}/approve")
async def approve_maintenance(suggestion_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    if not get_repository().decide_suggestion(user_id, suggestion_id, "approved"):
        raise HTTPException(status_code=404, detail="维护建议不存在或已经处理")
    return {"ok": True}


@router.post("/api/knowledge-maintenance/{suggestion_id}/reject")
async def reject_maintenance(suggestion_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    if not get_repository().decide_suggestion(user_id, suggestion_id, "rejected"):
        raise HTTPException(status_code=404, detail="维护建议不存在或已经处理")
    return {"ok": True}
