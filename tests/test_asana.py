import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "client", Path(__file__).parents[1] / "skills/asana/scripts/asana.py"
)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.log_patch = patch.object(client, "append_log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_audit_start_failure_blocks_api(self):
        client.append_log.side_effect = OSError("disk full")
        with patch.object(client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "operation was not started"):
                client.main(["me"])
            request.assert_not_called()

    def test_audit_redacts_content_and_retains_gid(self):
        with patch.object(
            client, "request", return_value={"data": {"gid": "123", "notes": "secret"}}
        ):
            client.main(["task", "--gid", "123", "--text", "secret"])
        records = [call.args[0] for call in client.append_log.call_args_list]
        self.assertEqual(records[0]["run_id"], records[1]["run_id"])
        self.assertEqual(records[1]["result_gid"], "123")
        self.assertNotIn("secret", client.json.dumps(records))

    def test_audit_finish_failure_preserves_result(self):
        client.append_log.side_effect = [None, OSError("disk full")]
        with patch.object(client, "request", return_value={"data": {"gid": "123"}}):
            result = client.main(["me"])
        self.assertEqual(result["status"], "audit_log_finish_failed")
        self.assertEqual(result["result"]["data"]["gid"], "123")

    def test_jsonl_append(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            work = Path(directory) / "work"
            work.mkdir()
            previous = os.getcwd()
            try:
                os.chdir(work)
                self.log_patch.stop()
                with patch.object(client.Path, "home", return_value=home):
                    client.append_log({"event": "started"})
                    client.append_log({"event": "finished"})
                rows = (home / ".agents/logs/asana.jsonl").read_text().splitlines()
                self.assertEqual(
                    [client.json.loads(r)["event"] for r in rows],
                    ["started", "finished"],
                )
                self.assertFalse((work / ".agents").exists())
            finally:
                self.log_patch.start()
                os.chdir(previous)

    def test_api_events_explain_update(self):
        responses = [
            {
                "data": {
                    "gid": "123",
                    "name": "モデル実装",
                    "assignee": None,
                    "due_on": None,
                }
            },
            {
                "data": {
                    "gid": "123",
                    "name": "モデル実装",
                    "assignee": {"gid": "456", "name": "担当者"},
                    "due_on": "2026-11-18",
                }
            },
            {
                "data": {
                    "gid": "123",
                    "name": "モデル実装",
                    "assignee": {"gid": "456", "name": "担当者"},
                    "due_on": "2026-11-18",
                }
            },
        ]
        with patch.object(client, "_request", side_effect=responses):
            client.main(
                [
                    "update",
                    "--gid",
                    "123",
                    "--data",
                    '{"assignee":"456","due_on":"2026-11-18"}',
                ]
            )
        rows = [c.args[0] for c in client.append_log.call_args_list]
        self.assertEqual(len({r["run_id"] for r in rows}), 1)
        calls = [r for r in rows if r["event"] == "api_started"]
        self.assertEqual([r["method"] for r in calls], ["GET", "PUT", "GET"])
        self.assertEqual(calls[1]["requested"]["assignee"], "456")
        observations = [r["observed"] for r in rows if r["event"] == "api_finished"]
        self.assertIsNone(observations[0]["assignee"])
        self.assertEqual(observations[-1]["assignee"]["name"], "担当者")

    def test_api_log_failure_blocks_write(self):
        client.append_log.side_effect = OSError("disk full")
        with patch.object(client, "_request") as network:
            with self.assertRaises(OSError):
                client.request("POST", "/tasks", data={"name": "test"})
            network.assert_not_called()

    def test_api_post_log_failure_preserves_result(self):
        client.append_log.side_effect = [None, OSError("disk full")]
        with patch.object(
            client, "_request", return_value={"data": {"gid": "123"}}
        ) as network:
            with self.assertRaises(client.AuditCompletionError) as caught:
                client.request("POST", "/tasks", data={"name": "test"})
            self.assertEqual(caught.exception.result["data"]["gid"], "123")
            self.assertEqual(network.call_count, 1)

    def test_redaction_preserves_business_content(self):
        with patch.dict(client.os.environ, {"ASANA_ACCESS_TOKEN": "credential"}):
            result = client.redact(
                {
                    "notes": "説明 credential",
                    "token": "credential",
                    "nested": [{"text": "コメント"}],
                }
            )
        self.assertEqual(result["notes"], "説明 [REDACTED]")
        self.assertNotIn("token", result)
        self.assertEqual(result["nested"][0]["text"], "コメント")

    def test_windows_lock_branch(self):
        import tempfile
        import types

        windows = types.SimpleNamespace(
            LK_LOCK=1, LK_UNLCK=2, locking=unittest.mock.Mock()
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.log_patch.stop()
            try:
                with patch.object(client.Path, "home", return_value=home):
                    with (
                        patch.object(client.os, "name", "nt"),
                        patch.dict("sys.modules", {"msvcrt": windows}),
                    ):
                        client.append_log({"event": "started"})
                self.assertEqual(
                    [c.args[1:] for c in windows.locking.call_args_list],
                    [(1, 1), (2, 1)],
                )
                self.assertTrue((home / ".agents/logs/asana.jsonl").is_file())
            finally:
                self.log_patch.start()

    def test_operation_summary_has_target_diff_reason(self):
        old = {
            "gid": "123",
            "name": "モデル実装",
            "assignee": None,
            "due_on": "2026-11-15",
            "workspace": {"gid": "10"},
            "projects": [{"gid": "20"}],
            "permalink_url": "https://example.test/123",
        }
        new = dict(old, assignee={"gid": "456", "name": "担当者"}, due_on="2026-11-18")
        with patch.object(
            client,
            "_request",
            side_effect=[{"data": old}, {"data": new}, {"data": new}],
        ):
            client.main(
                [
                    "update",
                    "--gid",
                    "123",
                    "--reason",
                    "担当変更と期限延期",
                    "--data",
                    '{"assignee":"456","due_on":"2026-11-18"}',
                ]
            )
        summary = client.append_log.call_args.args[0]
        self.assertEqual(summary["action"], "update_task")
        self.assertEqual(summary["target"]["task_name"], "モデル実装")
        self.assertEqual(summary["target"]["project_gid"], "20")
        self.assertEqual(summary["diff"]["before"]["due_on"], "2026-11-15")
        self.assertEqual(summary["diff"]["after"]["due_on"], "2026-11-18")
        self.assertEqual(
            summary["agent_context"]["trigger_prompt_summary"], "担当変更と期限延期"
        )
        self.assertFalse(summary["agent_context"]["dry_run"])

    def test_long_body_preview_and_token_redaction(self):
        with patch.dict(client.os.environ, {"ASANA_ACCESS_TOKEN": "credential"}):
            result = client.redact(
                {"notes": "credential" + "あ" * 100, "text": "い" * 100}
            )
        self.assertNotIn("credential", client.json.dumps(result))
        self.assertLessEqual(len(result["notes"]), 51)
        self.assertLessEqual(len(result["text"]), 51)

    def test_http_metadata_is_captured(self):
        import io

        response = unittest.mock.MagicMock()
        response.status = 200
        response.headers = {"X-Asana-Request-Id": "req123"}
        response.read.return_value = b'{"data":{"gid":"123","name":"me"}}'
        response.__enter__.return_value = response
        with patch.dict(client.os.environ, {"ASANA_ACCESS_TOKEN": "test"}):
            with patch.object(client.urllib.request, "urlopen", return_value=response):
                client.main(["me", "--reason", "接続確認"])
        meta = client.append_log.call_args.args[0]["api_meta"]
        self.assertEqual(meta["http_status"], 200)
        self.assertEqual(meta["asana_request_id"], "req123")

    def test_failed_update_does_not_claim_after(self):
        old = {"gid": "123", "name": "test", "due_on": "2026-11-15"}
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": old},
                client.APIError("permission_denied", 403, ["forbidden"]),
            ],
        ):
            with self.assertRaises(client.APIError):
                client.main(
                    ["update", "--gid", "123", "--data", '{"due_on":"2026-11-18"}']
                )
        record = client.append_log.call_args.args[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["diff"]["before"], {"due_on": "2026-11-15"})
        self.assertEqual(record["diff"]["after"], {})
        self.assertEqual(record["error_message"]["http_status"], 403)

    def test_retry_metadata_retains_429(self):
        import io

        response = unittest.mock.MagicMock()
        response.status = 200
        response.headers = {"X-Request-Id": "req-ok"}
        response.read.return_value = b'{"data":{"gid":"123"}}'
        response.__enter__.return_value = response
        error = client.urllib.error.HTTPError(
            client.BASE,
            429,
            "rate limit",
            {"Retry-After": "0", "X-Request-Id": "req-limit"},
            io.BytesIO(b'{"errors":[]}'),
        )
        with patch.dict(client.os.environ, {"ASANA_ACCESS_TOKEN": "test"}):
            with patch.object(
                client.urllib.request, "urlopen", side_effect=[error, response]
            ):
                client.main(["me", "--reason", "接続確認"])
        meta = client.append_log.call_args.args[0]["api_meta"]
        self.assertEqual([m["http_status"] for m in meta["attempts"]], [429, 200])
        self.assertEqual(meta["attempts"][0]["retry_after"], "0")
        self.assertTrue(
            any(
                c.args[0].get("event") == "api_retry"
                for c in client.append_log.call_args_list
            )
        )

    def test_persisted_schema_clips_body_and_redacts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.log_patch.stop()
            try:
                with patch.object(client.Path, "home", return_value=home):
                    with patch.dict(
                        client.os.environ, {"ASANA_ACCESS_TOKEN": "credential"}
                    ):
                        client.append_log(
                            {
                                "run_id": "test",
                                "requested": {
                                    "notes": "credential" + "あ" * 100,
                                    "text": "い" * 100,
                                },
                                "Authorization": "Bearer credential",
                            }
                        )
                content = (home / ".agents/logs/asana.jsonl").read_text(
                    encoding="utf-8"
                )
                row = client.json.loads(content)
                self.assertEqual(row["schema_version"], 3)
                self.assertNotIn("credential", content)
                self.assertNotIn("Authorization", row)
                self.assertLessEqual(len(row["requested"]["notes"]), 51)
                self.assertLessEqual(len(row["requested"]["text"]), 51)
            finally:
                self.log_patch.start()

    def test_resolve_email_and_verify_workspace(self):
        user = {
            "gid": "456",
            "name": "担当者",
            "email": "user+test@example.com",
            "workspaces": [{"gid": "20"}],
        }
        with patch.object(client, "request", return_value={"data": user}) as call:
            result = client.main(
                [
                    "resolve-user",
                    "--workspace",
                    "20",
                    "--email",
                    " user+test@example.com ",
                ]
            )
        self.assertEqual(call.call_args.args[1], "/users/user%2Btest%40example.com")
        self.assertEqual(result["data"]["gid"], "456")
        self.assertTrue(result["data"]["membership_verified"])
        summary = client.append_log.call_args.args[0]
        self.assertEqual(summary["action"], "resolve_user")
        self.assertEqual(summary["target"]["user_name"], "担当者")

    def test_resolve_rejects_other_workspace(self):
        with patch.object(
            client,
            "request",
            return_value={"data": {"gid": "456", "workspaces": [{"gid": "other"}]}},
        ):
            with self.assertRaisesRegex(RuntimeError, "user_not_in_workspace"):
                client.main(
                    ["resolve-user", "--workspace", "20", "--email", "user@example.com"]
                )

    def test_resolve_missing_membership_is_unverified(self):
        with patch.object(client, "request", return_value={"data": {"gid": "456"}}):
            with self.assertRaisesRegex(
                RuntimeError, "workspace_membership_unverified"
            ):
                client.main(
                    ["resolve-user", "--workspace", "20", "--email", "user@example.com"]
                )

    def test_resolve_validates_inputs_before_api(self):
        for args in (
            ["--email", "user@example.com"],
            ["--workspace", "20"],
            ["--workspace", "20", "--email", "not-an-email"],
        ):
            with self.subTest(args=args), patch.object(client, "request") as call:
                with self.assertRaises(SystemExit):
                    client.main(["resolve-user"] + args)
                call.assert_not_called()

    def test_resolve_preserves_permission_error(self):
        with patch.object(
            client, "request", side_effect=client.APIError("permission_denied", 403, [])
        ):
            with self.assertRaises(client.APIError):
                client.main(
                    ["resolve-user", "--workspace", "20", "--email", "user@example.com"]
                )

    def test_pagination(self):
        pages = [
            {"data": [{"gid": "1"}], "next_page": {"offset": "next"}},
            {"data": [{"gid": "2"}], "next_page": None},
        ]
        with patch.object(client, "request", side_effect=pages) as call:
            result = client.listing("/tasks", {}, 2)
            self.assertEqual(len(result["data"]), 2)
            self.assertFalse(result["has_more"])
            self.assertEqual(call.call_args.args[2]["offset"], "next")

    def test_clear_and_verify(self):
        with patch.object(
            client,
            "request",
            side_effect=[
                {"data": {}},
                {"data": {"gid": "1"}},
                {"data": {"due_on": None}},
            ],
        ) as call:
            client.main(["update", "--gid", "1", "--data", '{"due_on":null}'])
            self.assertEqual(call.call_args_list[1].kwargs["data"], {"due_on": None})
            self.assertEqual(call.call_args.args[0], "GET")

    def test_create_subtask(self):
        with patch.object(
            client,
            "request",
            side_effect=[
                {"data": {"gid": "parent"}},
                {"data": {"gid": "child"}},
                {"data": {"gid": "child", "parent": {"gid": "parent"}}},
            ],
        ) as call:
            result = client.main(
                [
                    "create-subtask",
                    "--gid",
                    "parent",
                    "--data",
                    '{"name":"child","assignee":"me","start_on":"2026-11-14","due_on":"2026-11-18"}',
                ]
            )
            self.assertEqual(
                call.call_args_list[1].args, ("POST", "/tasks/parent/subtasks")
            )
            self.assertEqual(call.call_args_list[1].kwargs["data"]["assignee"], "me")
            self.assertEqual(result["data"]["parent"]["gid"], "parent")

    def test_subtask_listing(self):
        with patch.object(
            client, "request", return_value={"data": [], "next_page": None}
        ) as call:
            client.main(["subtasks", "--gid", "parent"])
            self.assertEqual(call.call_args.args[1], "/tasks/parent/subtasks")

    def test_subtask_requires_parent(self):
        with patch.object(client, "request") as call:
            with self.assertRaises(SystemExit):
                client.main(["create-subtask", "--data", '{"name":"child"}'])
            call.assert_not_called()

    def test_verification_failure_retains_created_gid(self):
        with patch.object(
            client,
            "request",
            side_effect=[{"data": {"gid": "new"}}, RuntimeError("network_failure")],
        ) as call:
            result = client.main(["create", "--data", '{"name":"test"}'])
            self.assertEqual(result["status"], "written_but_unverified")
            self.assertEqual(result["gid"], "new")
            self.assertEqual([c.args[0] for c in call.call_args_list], ["POST", "GET"])

    def test_invalid_query_rejected_without_network(self):
        for query in ("[]", "null", "1", "broken"):
            with self.subTest(query=query), patch.object(client, "request") as call:
                with self.assertRaises(SystemExit):
                    client.main(["workspaces", "--query", query])
                call.assert_not_called()

    def test_api_error_redacts_token(self):
        import io

        error = client.urllib.error.HTTPError(
            client.BASE,
            400,
            "Bad request",
            {},
            io.BytesIO(b'{"errors":[{"message":"invalid due date secret-token"}]}'),
        )
        with patch.dict(client.os.environ, {"ASANA_ACCESS_TOKEN": "secret-token"}):
            with patch.object(client.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(client.APIError) as caught:
                    client.request("PUT", "/tasks/1", data={"due_on": "bad"})
        self.assertEqual(caught.exception.details["http_status"], 400)
        self.assertEqual(
            caught.exception.details["messages"], ["invalid due date [REDACTED]"]
        )

    def test_missing_token(self):
        with patch.dict(client.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ASANA_ACCESS_TOKEN"):
                client.request("GET", "/users/me")

    def test_uncertain_write_no_retry(self):
        with patch.dict(client.os.environ, {"ASANA_ACCESS_TOKEN": "test"}):
            with patch.object(
                client.urllib.request, "urlopen", side_effect=TimeoutError
            ) as call:
                with self.assertRaisesRegex(RuntimeError, "write_outcome_unknown"):
                    client.request("POST", "/tasks", data={"name": "test"})
                self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
