import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "reader", Path(__file__).parents[1] / "skills/asana/scripts/read_logs.py"
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTests(unittest.TestCase):
    def test_update_before_after_and_confirmation(self):
        rows = [
            {"event": "started", "operation": "update"},
            {
                "event": "api_started",
                "method": "GET",
                "path": "/tasks/1",
                "call_id": "a",
            },
            {
                "event": "api_finished",
                "call_id": "a",
                "outcome": "success",
                "observed": {"gid": "1", "name": "実装", "assignee": None},
            },
            {
                "event": "api_started",
                "method": "PUT",
                "path": "/tasks/1",
                "call_id": "b",
                "requested": {"assignee": "2", "completed": True},
            },
            {
                "event": "api_finished",
                "call_id": "b",
                "outcome": "success",
                "observed": {
                    "gid": "1",
                    "assignee": {"gid": "2", "name": "担当者"},
                    "completed": True,
                },
            },
            {"event": "finished", "outcome": "written_but_unverified"},
        ]
        text = reader.render_run("run", rows)
        self.assertIn("| 担当者 | 未設定／解除 |", text)
        self.assertIn("- 対象: 実装", text)
        self.assertIn("担当者 (GID: 2)", text)
        self.assertIn("反映確認未完了", text)
        self.assertIn("| 完了状態 | 未取得 | 完了 |", text)

    def test_incomplete_and_legacy(self):
        text = reader.render_run(
            "old", [{"event": "started", "operation": "create", "gid": "1"}]
        )
        self.assertIn("復元できません", text)
        self.assertIn("終了記録なし", text)

    def test_malformed_line_and_interleaving(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"run_id": "a", "event": "started"}),
                        json.dumps({"run_id": "b", "event": "started"}),
                        json.dumps({"run_id": "a", "event": "finished"}),
                        '{"broken":',
                    ]
                ),
                encoding="utf-8",
            )
            runs, warnings = reader.load(path)
        self.assertEqual(len(runs["a"]), 2)
        self.assertEqual(len(warnings), 1)

    def test_no_html_or_newline_injection(self):
        self.assertEqual(reader.inline("<script>\nx"), "&lt;script&gt; ↵ x")


if __name__ == "__main__":
    unittest.main()
