import os
import shutil
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main
from fastapi.testclient import TestClient


class PromptLibraryMoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="prompt-library-test-")
        self.original_path = main.PROMPT_LIBRARY_PATH
        main.PROMPT_LIBRARY_PATH = os.path.join(self.temp_dir, "prompt_libraries.json")
        main.save_prompt_libraries({
            "active_library_id": "move_test",
            "libraries": [{
                "id": "move_test",
                "name": "移动测试库",
                "categories": [
                    {"id": "camera_motion", "name": "摄影机运动"},
                    {"id": "lighting_style", "name": "灯光"},
                ],
                "items": [
                    {"id": "prompt_a", "name": "提示词 A", "category": "camera_motion", "positive": "A"},
                    {"id": "prompt_b", "name": "提示词 B", "category": "camera_motion", "positive": "B"},
                    {"id": "prompt_c", "name": "提示词 C", "category": "camera_motion", "positive": "C"},
                ],
            }],
        })

    def tearDown(self):
        main.PROMPT_LIBRARY_PATH = self.original_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_batch_move_only_updates_selected_items(self):
        response = self.client.post("/api/prompt-libraries/items/move", json={
            "library_id": "move_test",
            "ids": ["prompt_a", "prompt_b"],
            "category": "lighting_style",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["moved"], 2)
        library = next(item for item in response.json()["library"]["libraries"] if item["id"] == "move_test")
        categories = {item["id"]: item["category"] for item in library["items"]}
        self.assertEqual(categories["prompt_a"], "lighting_style")
        self.assertEqual(categories["prompt_b"], "lighting_style")
        self.assertEqual(categories["prompt_c"], "camera_motion")

    def test_batch_move_validates_selection_and_target_category(self):
        empty = self.client.post("/api/prompt-libraries/items/move", json={
            "library_id": "move_test", "ids": [], "category": "lighting_style",
        })
        self.assertEqual(empty.status_code, 400)
        invalid = self.client.post("/api/prompt-libraries/items/move", json={
            "library_id": "move_test", "ids": ["prompt_a"], "category": "missing",
        })
        self.assertEqual(invalid.status_code, 400)


if __name__ == "__main__":
    unittest.main()
