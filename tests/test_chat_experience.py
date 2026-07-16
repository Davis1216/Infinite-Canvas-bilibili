import asyncio
import os
import shutil
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class ChatExperienceTests(unittest.TestCase):
    def setUp(self):
        self.user_id = f"chat-experience-{uuid.uuid4().hex}"
        self.headers = {"X-User-ID": self.user_id, "Content-Type": "application/json"}
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        shutil.rmtree(main.user_dir(self.user_id), ignore_errors=True)
        shutil.rmtree(os.path.join(main.CHAT_RUN_DIR, self.user_id), ignore_errors=True)
        with main.CHAT_RUN_LOCK:
            for run_id in [key for key, value in main.CHAT_RUNS.items() if value.get("user_id") == self.user_id]:
                main.CHAT_RUNS.pop(run_id, None)

    def test_client_draft_is_not_persisted_and_empty_legacy_is_hidden(self):
        draft = main.new_conversation(self.user_id, "草稿", persist=False)
        self.assertFalse(os.path.exists(main.conversation_path(self.user_id, draft["id"])))
        empty = main.new_conversation(self.user_id, "旧空会话")
        self.assertTrue(os.path.exists(main.conversation_path(self.user_id, empty["id"])))
        self.assertEqual(main.list_conversations(self.user_id), [])

    def test_optional_builtin_providers_are_disabled_by_default(self):
        defaults = {item["id"]: item for item in main.default_api_providers()}
        self.assertFalse(defaults["modelscope"]["enabled"])
        self.assertFalse(defaults["runninghub"]["enabled"])

    def test_image_welcome_action_keeps_agent_model_switching_available(self):
        page_path = os.path.join(main.STATIC_DIR, "gpt-chat.html")
        with open(page_path, "r", encoding="utf-8") as handle:
            page = handle.read()
        action = page.split("function useWelcomeSuggestion(kind){", 1)[1].split("function sendJinniStarter", 1)[0]
        self.assertIn("setMode('agent')", action)
        self.assertIn("setModelPickerScope('image')", action)
        self.assertIn("setTimeout(() => toggleModelPicker(true), 0)", action)
        self.assertNotIn("setMode('image')", action)
        self.assertIn("config.has_ms_key", page)
        self.assertIn("providerReadyForSelection", page)

    def test_first_queued_message_creates_exactly_one_history_record(self):
        async def no_background_run(*args, **kwargs):
            return None

        with patch.object(main, "run_chat_background", new=no_background_run):
            response = self.client.post("/api/chat/runs", headers=self.headers, json={
                "message": "第一次发送", "client_request_id": uuid.uuid4().hex,
                "provider": "comfly", "model": "test-model",
            })
        self.assertEqual(response.status_code, 202, response.text)
        conversation = response.json()["conversation"]
        self.assertTrue(os.path.exists(main.conversation_path(self.user_id, conversation["id"])))
        listed = self.client.get("/api/conversations", headers=self.headers).json()["conversations"]
        self.assertEqual([item["id"] for item in listed], [conversation["id"]])

    def test_error_metadata_and_message_branches_are_preserved(self):
        diagnostic_id = "diag-test"
        error = main.ChatRunFailure(
            "temporary gateway error", status_code=503, exception_type="UpstreamHTTPError",
            upstream_request_id="req-123", retryable=True,
        )
        info = main.chat_run_error_info(error, diagnostic_id)
        self.assertTrue(info["retryable"])
        self.assertEqual(info["status_code"], 503)
        self.assertEqual(info["diagnostic_id"], diagnostic_id)

        conversation = {
            "id": uuid.uuid4().hex,
            "messages": [
                {"id": "u1", "role": "user", "content": "原问题"},
                {"id": "a1", "role": "assistant", "content": "原回答"},
            ],
        }
        graph = main.ensure_conversation_message_graph(conversation)
        graph["branches"].append({"id": "branch-new", "messages": [{"id": "u2", "role": "user", "content": "新问题"}]})
        graph["active_branch_id"] = "branch-new"
        conversation["messages"] = graph["branches"][-1]["messages"]
        main._sync_active_message_branch(conversation)
        self.assertEqual(len(graph["branches"]), 2)
        self.assertEqual(graph["branches"][0]["messages"][-1]["content"], "原回答")
        self.assertEqual(graph["branches"][1]["messages"][0]["content"], "新问题")

    def test_transient_stream_failure_retries_without_global_serialization(self):
        run_id = f"chat_{uuid.uuid4().hex}"
        conversation = main.new_conversation(self.user_id, "重试测试")
        payload = main.ChatRequest(
            conversation_id=conversation["id"], message="测试重试", client_request_id=uuid.uuid4().hex,
            provider="comfly", model="test-model", mode="chat",
        )
        main.save_chat_run({
            "id": run_id, "user_id": self.user_id, "conversation_id": conversation["id"],
            "status": "queued", "provider_id": "comfly", "model": "test-model",
            "max_attempts": 3, "attempts": [], "partial_content": "", "diagnostic_id": "diag-retry-test",
            "created_at": main.now_ms(), "updated_at": main.now_ms(),
        })
        calls = {"count": 0}

        class FakeResponse:
            @property
            def body_iterator(self):
                async def events():
                    yield 'data: {"type":"done","message":{"id":"a1","content":"成功"}}\n\n'
                return events()

        async def flaky_stream(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise main.ChatRunFailure("temporary", retryable=True, retry_after=0.01)
            return FakeResponse()

        with patch.object(main, "chat_stream", new=flaky_stream):
            asyncio.run(main.run_chat_background(run_id, payload, None, self.user_id))
        run = main.CHAT_RUNS[run_id]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["attempt"], 3)
        self.assertEqual(calls["count"], 3)
        self.assertEqual([item["status"] for item in run["attempts"]], ["failed", "failed", "succeeded"])


if __name__ == "__main__":
    unittest.main()
