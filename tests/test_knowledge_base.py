import time
import unittest
import uuid
import subprocess
import tempfile
import shutil
import sqlite3
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

import main
from knowledge_base.vectorstores.lancedb import LanceDbVectorStore
from knowledge_base.repositories.sqlite import KnowledgeRepository


class EmbeddedVectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.vector_dir = Path(tempfile.mkdtemp(prefix="jinni-vector-test-"))
        self.profile = {"id": uuid.uuid4().hex, "dimensions": 4}

    def tearDown(self):
        shutil.rmtree(self.vector_dir, ignore_errors=True)

    def test_local_persistence_filtering_and_dimension_validation(self):
        store = LanceDbVectorStore(self.profile, self.vector_dir)
        store.upsert([
            {"id": "chunk-a", "vector": [1, 0, 0, 0], "payload": {
                "chunk_id": "chunk-a", "user_id": "user-a", "knowledge_base_id": "kb-a",
                "document_id": "doc-a", "version_id": "v-a", "generation": 1,
            }},
            {"id": "chunk-b", "vector": [0, 1, 0, 0], "payload": {
                "chunk_id": "chunk-b", "user_id": "user-b", "knowledge_base_id": "kb-b",
                "document_id": "doc-b", "version_id": "v-b", "generation": 1,
            }},
        ])
        reopened = LanceDbVectorStore(self.profile, self.vector_dir)
        result = reopened.search([1, 0, 0, 0], {"user_id": "user-a", "knowledge_base_id": "kb-a"}, 5)
        self.assertEqual([item["payload"]["chunk_id"] for item in result], ["chunk-a"])
        self.assertEqual(reopened.count({"user_id": "user-a"}), 1)
        self.assertTrue(reopened.health()["ok"])
        with self.assertRaisesRegex(ValueError, "3 维向量"):
            reopened.search([1, 0, 0], {}, 1)

    def test_legacy_profile_schema_migrates_without_external_vector_fields(self):
        database_path = self.vector_dir / "legacy.sqlite3"
        with sqlite3.connect(database_path) as db:
            db.executescript("""
                CREATE TABLE embedding_profiles (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL, qdrant_url TEXT NOT NULL,
                    qdrant_api_key TEXT NOT NULL DEFAULT '', collection_prefix TEXT NOT NULL DEFAULT 'jinni_kb',
                    timeout_seconds REAL NOT NULL DEFAULT 60, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE knowledge_bases (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    embedding_profile_id TEXT NOT NULL DEFAULT '', active_generation INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'ready', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
            """)
        repository = KnowledgeRepository(database_path)
        profile = repository.create_embedding_profile("legacy-user", {
            "name": "本地嵌入", "base_url": "https://embedding.example/v1", "api_key": "secret",
            "model": "embedding", "dimensions": 4096, "batch_size": 32, "timeout_seconds": 60,
        })
        self.assertNotIn("qdrant_url", profile)
        self.assertNotIn("qdrant_api_key", profile)
        self.assertEqual(profile["batch_size"], 32)
        with repository.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(knowledge_bases)")}
        self.assertIn("vector_backend", columns)


class BundledPythonStartupTests(unittest.TestCase):
    def test_bundled_python_can_import_project_packages(self):
        root = Path(__file__).resolve().parent.parent
        bundled = root / "python" / "python.exe"
        if not bundled.exists():
            self.skipTest("当前环境没有内置 Python")
        runtime_probe = subprocess.run(
            [str(bundled), "-c", "import _socket"], cwd=str(root), capture_output=True, text=True, timeout=10,
        )
        if runtime_probe.returncode != 0:
            self.skipTest("仓库未包含完整的内置 Python 二进制运行时")
        result = subprocess.run(
            [str(bundled), "-c", "import runpy; runpy.run_path('main.py', run_name='startup_probe'); print('ok')"],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


class KnowledgeBaseApiTests(unittest.TestCase):
    def setUp(self):
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.user_id = f"kb-test-{uuid.uuid4().hex}"
        self.headers = {"X-User-ID": self.user_id}
        self.json_headers = {**self.headers, "Content-Type": "application/json"}
        self.knowledge_base_ids = []
        self.profile_ids = []

    def tearDown(self):
        for knowledge_base_id in self.knowledge_base_ids:
            self.client.delete(f"/api/knowledge-bases/{knowledge_base_id}", headers=self.headers)
        for profile_id in self.profile_ids:
            self.client.delete(f"/api/embedding-profiles/{profile_id}", headers=self.headers)
        self.client_context.__exit__(None, None, None)

    def create_base(self, name="测试知识库"):
        response = self.client.post("/api/knowledge-bases", headers=self.json_headers, json={
            "name": name, "description": "隔离与检索测试", "embedding_profile_id": "",
        })
        self.assertEqual(response.status_code, 200, response.text)
        knowledge_base = response.json()["knowledge_base"]
        self.knowledge_base_ids.append(knowledge_base["id"])
        return knowledge_base

    def wait_for_job(self, job_id, expected=("waiting_configuration", "completed", "failed")):
        deadline = time.time() + 5
        while time.time() < deadline:
            jobs = self.client.get("/api/knowledge-jobs", headers=self.headers).json()["jobs"]
            job = next(item for item in jobs if item["id"] == job_id)
            if job["status"] in expected:
                return job
            time.sleep(0.05)
        self.fail("知识库任务未在超时前结束")

    def test_crud_text_index_fts_and_user_isolation(self):
        knowledge_base = self.create_base()
        created = self.client.post(
            f"/api/knowledge-bases/{knowledge_base['id']}/entries",
            headers=self.json_headers,
            json={"title": "Orion 发布规范", "content": "ProjectOrion requires a cobalt approval code before publishing."},
        )
        self.assertEqual(created.status_code, 200, created.text)
        job = self.wait_for_job(created.json()["job"]["id"])
        self.assertEqual(job["status"], "waiting_configuration")

        result = self.client.post(
            f"/api/knowledge-bases/{knowledge_base['id']}/search",
            headers=self.json_headers,
            json={"query": "ProjectOrion", "limit": 5},
        )
        self.assertEqual(result.status_code, 200, result.text)
        citations = result.json()["result"]["citations"]
        self.assertTrue(citations)
        self.assertEqual(citations[0]["title"], "Orion 发布规范")
        self.assertEqual(citations[0]["knowledge_base_id"], knowledge_base["id"])
        chunk_preview = self.client.get(
            f"/api/knowledge-chunks/{citations[0]['chunk_id']}", headers=self.headers,
        )
        self.assertEqual(chunk_preview.status_code, 200, chunk_preview.text)
        self.assertIn("cobalt approval code", chunk_preview.json()["chunk"]["text"])
        chinese = self.client.post(
            f"/api/knowledge-bases/{knowledge_base['id']}/search", headers=self.json_headers,
            json={"query": "发布", "limit": 5},
        )
        self.assertTrue(chinese.json()["result"]["citations"], "中文子串应在无分词词典时仍可召回")

        reindex = self.client.post(f"/api/knowledge-bases/{knowledge_base['id']}/reindex", headers=self.headers)
        self.assertEqual(reindex.status_code, 200, reindex.text)
        time.sleep(0.3)
        detail = self.client.get(f"/api/knowledge-bases/{knowledge_base['id']}", headers=self.headers).json()["knowledge_base"]
        self.assertEqual(detail["active_generation"], 1, "没有嵌入配置时不得切换到不完整的新索引代次")
        frozen = self.client.post(
            f"/api/knowledge-bases/{knowledge_base['id']}/search", headers=self.json_headers,
            json={"query": "ProjectOrion", "limit": 5, "generation": 1},
        )
        self.assertTrue(frozen.json()["result"]["citations"], "旧索引代次应继续供历史会话检索")

        stranger = {"X-User-ID": f"stranger-{uuid.uuid4().hex}"}
        self.assertEqual(self.client.get(f"/api/knowledge-bases/{knowledge_base['id']}", headers=stranger).status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/knowledge-chunks/{citations[0]['chunk_id']}", headers=stranger).status_code, 404
        )
        self.assertEqual(self.client.get("/api/knowledge-bases", headers=stranger).json()["knowledge_bases"], [])

    def test_retrieval_evidence_is_frozen_on_supported_assistant_message(self):
        conversation = {
            "messages": [
                {"id": "u1", "role": "user", "content": "依据资料回答"},
                {"id": "a1", "role": "assistant", "content": "答案 [1]"},
            ],
            "last_knowledge_retrieval": {
                "query": "依据资料回答", "mode": "strict", "citations": [{
                    "chunk_id": "chunk-1", "knowledge_base_id": "base-1",
                    "document_id": "doc-1", "version_id": "version-1", "title": "资料",
                }],
            },
        }
        main._attach_pending_knowledge_citations(conversation)
        assistant = conversation["messages"][-1]
        self.assertEqual(assistant["knowledge_citations"][0]["chunk_id"], "chunk-1")
        self.assertEqual(assistant["knowledge_mode"], "strict")
        self.assertEqual(conversation["last_knowledge_retrieval"]["assistant_message_id"], "a1")

    def test_jinni_strict_binding_and_frozen_generation(self):
        knowledge_base = self.create_base("严格回答库")
        payload = {
            "name": "规范问答", "description": "仅引用规范", "instructions": "只回答有依据的内容。",
            "starters": [], "capabilities": {"chat": True}, "knowledge_files": [],
            "knowledge_capabilities": {"augment": True, "strict": True, "maintain": True},
            "knowledge_base_ids": [knowledge_base["id"]],
            "strict_knowledge_base_id": knowledge_base["id"], "default_knowledge_mode": "strict",
        }
        created = self.client.post("/api/jinnis", headers=self.json_headers, json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        jinni = created.json()["jinni"]
        self.addCleanup(lambda: self.client.delete(f"/api/jinnis/{jinni['id']}", headers=self.headers))
        conversation = self.client.post(f"/api/jinnis/{jinni['id']}/conversations", headers=self.headers).json()["conversation"]
        self.assertEqual(conversation["knowledge_mode"], "strict")
        self.assertEqual(conversation["knowledge_generations"][knowledge_base["id"]], 1)
        self.assertEqual(conversation["jinni_snapshot"]["strict_knowledge_base_id"], knowledge_base["id"])

        forbidden = self.client.patch(
            f"/api/conversations/{conversation['id']}", headers=self.json_headers,
            json={"strict_knowledge_base_id": uuid.uuid4().hex},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_per_capability_jinni_config_and_ordinary_conversation_context(self):
        first = self.create_base("品牌资料")
        second = self.create_base("维护资料")
        invalid_jinni = self.client.post("/api/jinnis", headers=self.json_headers, json={
            "name": "无范围助理", "instructions": "测试", "knowledge_config": {
                "default_mode": "none", "augment": {"enabled": True, "knowledge_base_ids": []},
                "strict": {"enabled": False, "knowledge_base_id": ""},
                "maintain": {"enabled": False, "knowledge_base_ids": []},
            },
        })
        self.assertEqual(invalid_jinni.status_code, 400)
        created = self.client.post("/api/jinnis", headers=self.json_headers, json={
            "name": "知识助理", "instructions": "依据授权范围工作。", "knowledge_config": {
                "default_mode": "augment",
                "augment": {"enabled": True, "knowledge_base_ids": [first["id"]]},
                "strict": {"enabled": True, "knowledge_base_id": second["id"]},
                "maintain": {"enabled": True, "knowledge_base_ids": [second["id"]]},
            },
        })
        self.assertEqual(created.status_code, 200, created.text)
        jinni = created.json()["jinni"]
        self.addCleanup(lambda: self.client.delete(f"/api/jinnis/{jinni['id']}", headers=self.headers))
        self.assertEqual(jinni["knowledge_config"]["augment"]["knowledge_base_ids"], [first["id"]])
        self.assertEqual(jinni["knowledge_config"]["strict"]["knowledge_base_id"], second["id"])
        conversation = self.client.post(
            f"/api/jinnis/{jinni['id']}/conversations", headers=self.headers
        ).json()["conversation"]
        self.assertEqual(conversation["knowledge_mode"], "augment")
        locked = self.client.patch(
            f"/api/conversations/{conversation['id']}", headers=self.json_headers,
            json={"knowledge_context": {"mode": "strict", "knowledge_base_ids": [first["id"]]}},
        )
        self.assertEqual(locked.status_code, 400)

        ordinary = self.client.post("/api/conversations", headers=self.json_headers, json={"title": "普通知识问答"}).json()["conversation"]
        self.addCleanup(lambda: self.client.delete(f"/api/conversations/{ordinary['id']}", headers=self.headers))
        bound = self.client.patch(
            f"/api/conversations/{ordinary['id']}", headers=self.json_headers,
            json={"knowledge_context": {"mode": "strict", "knowledge_base_ids": [first["id"], second["id"]]}},
        )
        self.assertEqual(bound.status_code, 200, bound.text)
        context = bound.json()["conversation"]["knowledge_context"]
        self.assertEqual(context["mode"], "strict")
        self.assertEqual(context["generations"], {first["id"]: 1, second["id"]: 1})
        main.get_knowledge_repository().activate_generation(self.user_id, first["id"], 2)
        loaded = self.client.get(f"/api/conversations/{ordinary['id']}", headers=self.headers).json()["conversation"]
        self.assertEqual(loaded["knowledge_updates_available"], [first["id"]])
        refreshed = self.client.patch(
            f"/api/conversations/{ordinary['id']}", headers=self.json_headers,
            json={"refresh_knowledge_generations": True},
        ).json()["conversation"]
        self.assertEqual(refreshed["knowledge_context"]["generations"][first["id"]], 2)
        self.assertEqual(refreshed["knowledge_updates_available"], [])
        invalid = self.client.patch(
            f"/api/conversations/{ordinary['id']}", headers=self.json_headers,
            json={"knowledge_context": {"mode": "augment", "knowledge_base_ids": [uuid.uuid4().hex]}},
        )
        self.assertEqual(invalid.status_code, 400)

        async def no_background_run(*args, **kwargs):
            return None
        with patch.object(main, "run_chat_background", new=no_background_run):
            first_message = self.client.post("/api/chat/runs", headers=self.json_headers, json={
                "message": "依据资料回答", "client_request_id": uuid.uuid4().hex,
                "knowledge_context": {"mode": "augment", "knowledge_base_ids": [second["id"]]},
            })
        self.assertEqual(first_message.status_code, 202, first_message.text)
        drafted = first_message.json()["conversation"]
        self.addCleanup(lambda: self.client.delete(f"/api/conversations/{drafted['id']}", headers=self.headers))
        self.assertEqual(drafted["knowledge_context"]["knowledge_base_ids"], [second["id"]])

    def test_document_preview_views_and_isolation(self):
        knowledge_base = self.create_base("预览测试")
        created = self.client.post(
            f"/api/knowledge-bases/{knowledge_base['id']}/entries", headers=self.json_headers,
            json={"title": "安全 Markdown", "content": "# 标题\n\n<script>alert(1)</script>\n\n正文内容"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        document_id = created.json()["document"]["id"]
        self.wait_for_job(created.json()["job"]["id"])
        preview = self.client.get(
            f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document_id}/preview?view=read",
            headers=self.headers,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual(body["format"], "md")
        self.assertTrue(any("正文内容" in block["text"] for block in body["blocks"]))
        self.assertNotIn("content_path", body["document"])
        chunks = self.client.get(
            f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document_id}/preview?view=chunks",
            headers=self.headers,
        ).json()["blocks"]
        self.assertTrue(chunks)
        stranger = {"X-User-ID": f"stranger-{uuid.uuid4().hex}"}
        denied = self.client.get(
            f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document_id}/preview", headers=stranger,
        )
        self.assertEqual(denied.status_code, 404)

    def test_embedding_profile_secrets_are_not_returned(self):
        response = self.client.post("/api/embedding-profiles", headers=self.json_headers, json={
            "name": "4092 维模型", "base_url": "https://embedding.example/v1", "api_key": "secret-embedding",
            "model": "best-embedding", "dimensions": 4092, "batch_size": 16, "timeout_seconds": 30,
        })
        self.assertEqual(response.status_code, 200, response.text)
        profile = response.json()["profile"]
        self.profile_ids.append(profile["id"])
        self.assertNotIn("api_key", profile)
        self.assertTrue(profile["api_key_configured"])
        self.assertEqual(profile["batch_size"], 16)
        listed = self.client.get("/api/embedding-profiles", headers=self.headers).json()["profiles"]
        self.assertEqual(listed[0]["id"], profile["id"])
        health = self.client.get("/api/vector-store/health", headers=self.headers).json()
        self.assertEqual(health["backend"], "lancedb")
        self.assertTrue(health["embedded"])
        self.assertEqual(health["profiles"][0]["dimensions"], 4092)


if __name__ == "__main__":
    unittest.main()
