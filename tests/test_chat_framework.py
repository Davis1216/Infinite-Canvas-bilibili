import asyncio
import json
import os
import shutil
import sys
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient


class ChatFrameworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()
        cls.original_chat_stream = main.chat_stream

        async def fake_chat_stream(payload, request, x_user_id=""):
            user_id = main.safe_user_id(x_user_id, request)
            conversation = main.load_conversation(user_id, payload.conversation_id)
            user_message = {
                "id": uuid.uuid4().hex,
                "role": "user",
                "content": payload.message,
                "created_at": main.now_ms(),
                "attachments": [],
                "mode": "chat",
            }
            conversation["messages"].append(user_message)
            main.save_conversation(user_id, conversation)

            async def stream():
                yield main.sse_event({"type": "meta", "conversation": conversation})
                await asyncio.sleep(0.8 if "慢任务" in payload.message else 0.12)
                yield main.sse_event({"type": "delta", "delta": "测试回复"})
                assistant = {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "content": "测试回复",
                    "created_at": main.now_ms(),
                    "model": "test-model",
                    "mode": "chat",
                }
                conversation["messages"].append(assistant)
                conversation["updated_at"] = main.now_ms()
                main.save_conversation(user_id, conversation)
                yield main.sse_event({"type": "done", "conversation": conversation, "message": assistant})

            return StreamingResponse(stream(), media_type="text/event-stream")

        main.chat_stream = fake_chat_stream

    @classmethod
    def tearDownClass(cls):
        main.chat_stream = cls.original_chat_stream
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        self.user_id = f"test-chat-{uuid.uuid4().hex}"
        self.headers = {"X-User-ID": self.user_id, "Content-Type": "application/json"}

    def tearDown(self):
        for run in main.list_chat_runs(self.user_id, active_only=True):
            task = main.CHAT_RUN_TASKS.get(run["id"])
            if task and not task.done():
                task.cancel()
        with main.CHAT_RUN_LOCK:
            for run_id in [key for key, value in main.CHAT_RUNS.items() if value.get("user_id") == self.user_id]:
                main.CHAT_RUNS.pop(run_id, None)
                main.CHAT_RUN_TASKS.pop(run_id, None)
        shutil.rmtree(os.path.join(main.CONVERSATION_DIR, self.user_id), ignore_errors=True)
        shutil.rmtree(os.path.join(main.CHAT_RUN_DIR, self.user_id), ignore_errors=True)
        shutil.rmtree(os.path.join(main.JINNI_DIR, self.user_id), ignore_errors=True)

    def create_conversation(self, title="新对话"):
        response = self.client.post("/api/conversations", headers=self.headers, json={"title": title})
        self.assertEqual(response.status_code, 200)
        return response.json()["conversation"]

    def wait_for_status(self, run_id, expected, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = self.client.get(f"/api/chat/runs/{run_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            run = response.json()["run"]
            if run["status"] in expected:
                return run
            time.sleep(0.03)
        self.fail(f"任务 {run_id} 未进入状态 {expected}")

    def test_conversation_metadata_search_and_legacy_defaults(self):
        conversation = self.create_conversation("旧会话")
        path = main.conversation_path(self.user_id, conversation["id"])
        with open(path, "r", encoding="utf-8") as file:
            legacy = json.load(file)
        legacy.pop("pinned", None)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(legacy, file, ensure_ascii=False)

        response = self.client.patch(
            f"/api/conversations/{conversation['id']}",
            headers=self.headers,
            json={"title": "中文会话名称", "pinned": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["conversation"]["pinned"])
        found = self.client.get("/api/conversations", headers=self.headers, params={"q": "中文"}).json()["conversations"]
        self.assertEqual([item["id"] for item in found], [conversation["id"]])
        self.assertTrue(found[0]["pinned"])

    def test_cross_conversation_parallel_same_conversation_serial_and_idempotent(self):
        first = self.create_conversation("会话一")
        second = self.create_conversation("会话二")
        first_payload = {
            "conversation_id": first["id"],
            "client_request_id": uuid.uuid4().hex,
            "message": "第一条消息",
            "mode": "chat",
            "provider": "test",
            "model": "test-model",
        }
        second_payload = {
            "conversation_id": second["id"],
            "client_request_id": uuid.uuid4().hex,
            "message": "第二条消息",
            "mode": "chat",
            "provider": "test",
            "model": "test-model",
        }
        first_response = self.client.post("/api/chat/runs", headers=self.headers, json=first_payload)
        second_response = self.client.post("/api/chat/runs", headers=self.headers, json=second_payload)
        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)

        conflict_payload = dict(first_payload, client_request_id=uuid.uuid4().hex, message="不应并行")
        conflict = self.client.post("/api/chat/runs", headers=self.headers, json=conflict_payload)
        self.assertEqual(conflict.status_code, 409)

        duplicate = self.client.post("/api/chat/runs", headers=self.headers, json=first_payload)
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(duplicate.json()["run"]["id"], first_response.json()["run"]["id"])

        first_run = self.wait_for_status(first_response.json()["run"]["id"], {"succeeded"})
        second_run = self.wait_for_status(second_response.json()["run"]["id"], {"succeeded"})
        self.assertEqual(first_run["partial_content"], "测试回复")
        self.assertEqual(second_run["partial_content"], "测试回复")
        self.assertEqual(len(main.load_conversation(self.user_id, first["id"])["messages"]), 2)
        self.assertEqual(len(main.load_conversation(self.user_id, second["id"])["messages"]), 2)

    def test_cancel_and_restart_recovery(self):
        conversation = self.create_conversation("取消测试")
        payload = {
            "conversation_id": conversation["id"],
            "client_request_id": uuid.uuid4().hex,
            "message": "慢任务",
            "mode": "chat",
            "provider": "test",
            "model": "test-model",
        }
        created = self.client.post("/api/chat/runs", headers=self.headers, json=payload).json()["run"]
        cancelled = self.client.post(f"/api/chat/runs/{created['id']}/cancel", headers=self.headers).json()["run"]
        self.assertEqual(cancelled["status"], "cancelled")

        interrupted_id = f"chat_{uuid.uuid4().hex}"
        main.save_chat_run({
            "id": interrupted_id,
            "user_id": self.user_id,
            "conversation_id": conversation["id"],
            "client_request_id": uuid.uuid4().hex,
            "status": "running",
            "partial_content": "部分回复",
            "error": "",
            "created_at": main.now_ms(),
            "updated_at": main.now_ms(),
        })
        main.recover_chat_runs()
        recovered = main.CHAT_RUNS[interrupted_id]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["partial_content"], "部分回复")

    def test_jinni_crud_user_isolation_snapshot_and_delete(self):
        payload = {
            "name": "创意导演",
            "description": "帮助完善视觉创意",
            "instructions": "你是一名资深创意导演。",
            "starters": ["帮我构思海报", "分析这张参考图"],
            "provider_id": "test",
            "chat_model": "test-model",
            "image_provider_id": "test",
            "image_model": "test-image",
            "capabilities": {"generate_image": True, "edit_image": True},
            "knowledge_files": [],
        }
        created = self.client.post("/api/jinnis", headers=self.headers, json=payload)
        self.assertEqual(created.status_code, 200)
        jinni = created.json()["jinni"]
        self.assertEqual(jinni["name"], "创意导演")
        self.assertTrue(jinni["capabilities"]["chat"])

        listed = self.client.get("/api/jinnis", headers=self.headers, params={"q": "视觉"})
        self.assertEqual([item["id"] for item in listed.json()["jinnis"]], [jinni["id"]])
        stranger_headers = {"X-User-ID": f"other-{uuid.uuid4().hex}"}
        self.assertEqual(self.client.get("/api/jinnis", headers=stranger_headers).json()["jinnis"], [])

        conversation_response = self.client.post(f"/api/jinnis/{jinni['id']}/conversations", headers=self.headers)
        self.assertEqual(conversation_response.status_code, 200)
        conversation = conversation_response.json()["conversation"]
        self.assertEqual(conversation["jinni_snapshot"]["name"], "创意导演")

        updated = self.client.patch(
            f"/api/jinnis/{jinni['id']}", headers=self.headers,
            json={"name": "新版创意导演", "instructions": "使用新版本指令。"},
        )
        self.assertEqual(updated.status_code, 200)
        frozen = main.load_conversation(self.user_id, conversation["id"])
        self.assertEqual(frozen["jinni_snapshot"]["name"], "创意导演")
        fresh = self.client.post(f"/api/jinnis/{jinni['id']}/conversations", headers=self.headers).json()["conversation"]
        self.assertEqual(fresh["jinni_snapshot"]["name"], "新版创意导演")

        deleted = self.client.delete(f"/api/jinnis/{jinni['id']}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(main.load_conversation(self.user_id, conversation["id"])["jinni_snapshot"]["instructions"], "你是一名资深创意导演。")
        records = self.client.get("/api/conversations", headers=self.headers).json()["conversations"]
        self.assertTrue(next(item for item in records if item["id"] == conversation["id"])["jinni"]["deleted"])

    def test_jinni_knowledge_limits_runtime_and_model_override(self):
        knowledge_name = f"jinni_{uuid.uuid4().hex}.txt"
        knowledge_path = os.path.join(main.OUTPUT_INPUT_DIR, knowledge_name)
        os.makedirs(main.OUTPUT_INPUT_DIR, exist_ok=True)
        with open(knowledge_path, "w", encoding="utf-8") as file:
            file.write("品牌主色是深海蓝。")
        self.addCleanup(lambda: os.path.exists(knowledge_path) and os.remove(knowledge_path))
        payload = {
            "name": "品牌助手",
            "instructions": "只依据品牌资料回答。",
            "provider_id": "test",
            "chat_model": "base-model",
            "capabilities": {"generate_image": False, "edit_image": False},
            "knowledge_files": [{"url": f"/assets/input/{knowledge_name}", "name": knowledge_name, "kind": "file"}],
        }
        jinni = self.client.post("/api/jinnis", headers=self.headers, json=payload).json()["jinni"]
        conversation = self.client.post(f"/api/jinnis/{jinni['id']}/conversations", headers=self.headers).json()["conversation"]

        patched = self.client.patch(
            f"/api/conversations/{conversation['id']}", headers=self.headers,
            json={
                "runtime_provider_id": "test-override",
                "runtime_model": "override-model",
                "runtime_image_provider_id": "image-override",
                "runtime_image_model": "override-image-model",
            },
        )
        self.assertEqual(patched.status_code, 200)
        stored = patched.json()["conversation"]
        request = main.ChatRequest(conversation_id=stored["id"], message="主色是什么", mode="agent")
        main.prepare_jinni_chat_payload(request, stored)
        self.assertEqual(request.mode, "chat")
        self.assertEqual(request.provider, "test-override")
        self.assertEqual(request.model, "override-model")
        self.assertEqual(request.image_provider, "image-override")
        self.assertEqual(request.image_model, "override-image-model")
        self.assertIn("品牌主色是深海蓝", request.system_prompt)
        self.assertEqual(main.enforce_jinni_agent_action(stored, "generate_image"), "chat")
        self.assertEqual(main.enforce_jinni_agent_action(stored, "edit_image"), "chat")

        too_many = dict(payload)
        too_many["name"] = "文件过多"
        too_many["knowledge_files"] = [
            {"url": f"/assets/input/{i}.txt", "name": f"{i}.txt", "kind": "file"}
            for i in range(main.JINNI_KNOWLEDGE_MAX + 1)
        ]
        rejected = self.client.post("/api/jinnis", headers=self.headers, json=too_many)
        self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
