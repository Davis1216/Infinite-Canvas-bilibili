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

    def test_export_selected_prompts_uses_versioned_package_without_mutating_groups(self):
        before = main.load_prompt_libraries()
        response = self.client.post("/api/prompt-libraries/items/export", json={
            "library_id": "move_test", "ids": ["prompt_a", "prompt_c"],
        })
        self.assertEqual(response.status_code, 200)
        package = response.json()
        self.assertEqual(package["format"], "jinni.prompt-pack")
        self.assertEqual(package["version"], 1)
        self.assertEqual([item["name"] for item in package["prompts"]], ["提示词 A", "提示词 C"])
        self.assertEqual(package["prompts"][0]["category"]["name"], "摄影机运动")
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        after = main.load_prompt_libraries()
        self.assertEqual(before["libraries"][0]["categories"], after["libraries"][0]["categories"])

    def test_import_preview_and_execute_preserve_library_and_category_tree(self):
        package = {
            "format": "jinni.prompt-pack",
            "version": 1,
            "prompts": [
                {"name": "按 ID 映射", "positive": "new prompt id", "category": {"id": "lighting_style", "name": "旧名称"}},
                {"name": "按名称映射", "positive": "new prompt name", "category": {"id": "legacy", "name": "摄影机运动"}},
                {"name": "兜底映射", "positive": "new prompt fallback", "category": {"id": "missing", "name": "不存在分组"}},
                {"name": "无效条目", "positive": ""},
            ],
        }
        before = main.load_prompt_libraries()
        before_tree = [(lib["id"], lib["name"], lib["categories"]) for lib in before["libraries"]]
        preview = self.client.post("/api/prompt-libraries/items/import", json={
            "library_id": "move_test", "package": package, "fallback_category": "lighting_style", "dry_run": True,
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["preview"]["importable"], 3)
        self.assertEqual(preview.json()["preview"]["invalid"], 1)
        self.assertEqual(preview.json()["preview"]["unmatched"], 0)
        self.assertEqual(before, main.load_prompt_libraries())

        imported = self.client.post("/api/prompt-libraries/items/import", json={
            "library_id": "move_test", "package": package, "fallback_category": "lighting_style", "dry_run": False,
        })
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["result"]["imported"], 3)
        after = main.load_prompt_libraries()
        after_tree = [(lib["id"], lib["name"], lib["categories"]) for lib in after["libraries"]]
        self.assertEqual(before_tree, after_tree)
        library = next(lib for lib in after["libraries"] if lib["id"] == "move_test")
        imported_items = {item["name"]: item for item in library["items"]}
        self.assertEqual(imported_items["按 ID 映射"]["category"], "lighting_style")
        self.assertEqual(imported_items["按名称映射"]["category"], "camera_motion")
        self.assertEqual(imported_items["兜底映射"]["category"], "lighting_style")

    def test_import_requires_existing_fallback_and_skips_duplicates(self):
        package = {"prompts": [
            {"name": "提示词 A", "positive": "A", "category": {"id": "missing", "name": "不存在"}},
            {"name": "新增重复", "positive": "A", "category": {"id": "camera_motion", "name": "摄影机运动"}},
        ]}
        no_fallback = self.client.post("/api/prompt-libraries/items/import", json={
            "library_id": "move_test", "package": package, "dry_run": True,
        })
        self.assertEqual(no_fallback.status_code, 200)
        self.assertTrue(no_fallback.json()["preview"]["requires_fallback"])

        invalid_fallback = self.client.post("/api/prompt-libraries/items/import", json={
            "library_id": "move_test", "package": package, "fallback_category": "not-there", "dry_run": True,
        })
        self.assertEqual(invalid_fallback.status_code, 400)

        duplicate = self.client.post("/api/prompt-libraries/items/import", json={
            "library_id": "move_test", "package": package, "fallback_category": "camera_motion", "dry_run": True,
        })
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["preview"]["duplicates"], 1)

        before_categories = main.load_prompt_libraries()["libraries"][0]["categories"]
        copied = self.client.post("/api/prompt-libraries/items/import", json={
            "library_id": "move_test",
            "package": package,
            "fallback_category": "camera_motion",
            "duplicate_policy": "copy",
            "dry_run": False,
        })
        self.assertEqual(copied.status_code, 200)
        self.assertEqual(copied.json()["result"]["imported"], 2)
        self.assertEqual(before_categories, main.load_prompt_libraries()["libraries"][0]["categories"])
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.temp_dir)))

    def test_prompt_pack_ui_exposes_single_bulk_and_preview_controls(self):
        with open(os.path.join(ROOT, "static", "asset-manager.html"), "r", encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(ROOT, "static", "js", "asset-manager.js"), "r", encoding="utf-8") as handle:
            script = handle.read()
        with open(os.path.join(ROOT, "static", "css", "asset-manager.css"), "r", encoding="utf-8") as handle:
            styles = handle.read()
        self.assertIn('id="promptPackInput"', html)
        for marker in (
            "data-prompt-pack-import",
            "data-prompt-pack-export-all",
            "data-prompt-pack-export-selected",
            "data-prompt-pack-export-one",
            "data-prompt-pack-confirm",
            "分组保护已启用",
        ):
            self.assertIn(marker, script)
        self.assertIn(".prompt-pack-card", styles)
        self.assertIn(".prompt-pack-map-list", styles)


if __name__ == "__main__":
    unittest.main()
