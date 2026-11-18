import importlib.util
import tempfile
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

    def test_comment_mentions_escape_text_and_confirm_user_links(self):
        task = {"gid": "1"}
        story = {
            "gid": "4",
            "text": "@A @B 確認 ✅",
            "html_text": '<body><a data-asana-type="user" data-asana-gid="2">@A</a> '
            '<a data-asana-type="user" data-asana-gid="3">@B</a> 確認 ✅</body>',
        }
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": task},
                {"data": {"gid": "2"}},
                {"data": {"gid": "3"}},
                {"data": {"gid": "4"}},
                {"data": story},
            ],
        ) as api:
            result = client.main(
                [
                    "comment",
                    "--gid",
                    "1",
                    "--text",
                    "確認 ✅ <a> &",
                    "--mention",
                    "2",
                    "--mention",
                    "3",
                    "--mention",
                    "2",
                ]
            )
        self.assertEqual(result["mentions"], ["2", "3"])
        self.assertEqual(
            api.call_args_list[3].args,
            (
                "POST",
                "/tasks/1/stories",
                {"opt_fields": "gid,text,html_text"},
                {
                    "html_text": '<body><a data-asana-gid="2"/> <a data-asana-gid="3"/> 確認 ✅ &lt;a&gt; &amp;</body>'
                },
            ),
        )
        self.assertEqual(api.call_args.args[1], "/stories/4")
        row = client.append_log.call_args.args[0]
        self.assertEqual(row["requested"]["mentions"], ["2", "3"])
        self.assertEqual(row["diff"]["after"]["mentions"], ["2", "3"])

    def test_comment_unconfirmed_mention_does_not_repost(self):
        story = {
            "gid": "4",
            "html_text": '<body><a data-asana-type="task" data-asana-gid="2">Task</a></body>',
        }
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"gid": "1"}},
                {"data": {"gid": "2"}},
                {"data": story},
            ],
        ) as api:
            result = client.main(
                ["comment", "--gid", "1", "--text", "確認", "--mention", "2"]
            )
        self.assertEqual(result["status"], "written_but_unverified")
        self.assertEqual(result["gid"], "4")
        self.assertEqual(
            [call.args[0] for call in api.call_args_list], ["GET", "GET", "POST"]
        )

    def test_upload_attachment_checks_uploaded_gid_and_logs_only_metadata(self):
        attachment = {
            "gid": "3",
            "name": "spec.txt",
            "view_url": "https://example.com/file",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / attachment["name"]
            path.write_bytes(b"file payload")
            with patch.object(
                client,
                "_request",
                side_effect=[
                    {"data": {"gid": "2"}},
                    {"data": {"gid": "3"}},
                    {"data": [{"gid": "4", "name": "spec.txt"}, attachment]},
                ],
            ) as api:
                result = client.main(
                    ["upload-attachment", "--gid", "2", "--file", str(path)]
                )
        self.assertEqual(result["data"], attachment)
        self.assertEqual(
            api.call_args_list[1].args,
            (
                "POST",
                "/attachments",
                None,
                {"parent": "2", "name": "spec.txt"},
            ),
        )
        self.assertEqual(api.call_args_list[1].kwargs, {"upload": b"file payload"})
        logs = str(client.append_log.call_args_list)
        for excluded in (str(path), "file payload", attachment["view_url"]):
            self.assertNotIn(excluded, logs)
        self.assertEqual(
            client.append_log.call_args.args[0]["target"]["attachment_gid"], "3"
        )

    def test_delete_attachment_requires_membership_and_verifies_absence(self):
        args = ["delete-attachment", "--gid", "2", "--attachment", "3"]
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"gid": "2"}},
                {"data": [{"gid": "4", "name": "other.txt"}]},
            ],
        ) as api:
            with self.assertRaisesRegex(ValueError, "attachment_not_found_on_task"):
                client.main(args)
            self.assertEqual(
                [call.args[0] for call in api.call_args_list], ["GET", "GET"]
            )
        item = {"gid": "3", "name": "spec.txt"}
        for remaining in ([], [item]):
            with self.subTest(remaining=remaining):
                with patch.object(
                    client,
                    "_request",
                    side_effect=[
                        {"data": {"gid": "2"}},
                        {"data": [item]},
                        {"data": {}},
                        {"data": remaining},
                    ],
                ) as api:
                    result = client.main(args)
                self.assertEqual(
                    api.call_args_list[2].args, ("DELETE", "/attachments/3", None, None)
                )
                self.assertEqual(len(api.call_args_list), 4)
                if remaining:
                    self.assertEqual(result["status"], "written_but_unverified")
                else:
                    self.assertTrue(result["data"]["deleted"])

    def test_attachment_multipart_preserves_japanese_filename_and_bytes(self):
        body, content_type = client.attachment_body("2", "資料.png", b"\x00\xff")
        boundary = content_type.split("boundary=")[1].encode("ascii")
        self.assertIn(b'name="parent"\r\n\r\n2\r\n', body)
        self.assertIn('filename="資料.png"'.encode("utf-8"), body)
        self.assertIn(b"Content-Type: image/png\r\n\r\n\x00\xff", body)
        self.assertTrue(body.endswith(b"\r\n--" + boundary + b"--\r\n"))

    def test_dependency_direction_and_confirmed_state(self):
        prerequisite = {"gid": "1", "name": "原稿", "completed": False}
        task = {"gid": "2", "name": "校正"}
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": prerequisite},
                {"data": task},
                {"data": []},
                {"data": {}},
                {"data": [prerequisite]},
            ],
        ) as api:
            result = client.main(["add-dependency", "--gid", "2", "--depends-on", "1"])
        self.assertEqual(
            api.call_args_list[3].args,
            (
                "POST",
                "/tasks/2/addDependencies",
                None,
                {"dependencies": ["1"]},
            ),
        )
        self.assertEqual(result["data"], [prerequisite])
        row = client.append_log.call_args.args[0]
        self.assertEqual(row["target"]["task_gid"], "2")
        self.assertEqual(row["target"]["dependency_gid"], "1")
        self.assertEqual(row["diff"]["after"], {"dependencies": ["1"]})

    def test_dependency_on_later_page_does_not_write(self):
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"gid": "1"}},
                {"data": {"gid": "2"}},
                {"data": [{"gid": "3"}], "next_page": {"offset": "page2"}},
                {"data": [{"gid": "1"}]},
            ],
        ) as api:
            result = client.main(["add-dependency", "--gid", "2", "--depends-on", "1"])
        self.assertTrue(result["already_set"])
        self.assertEqual(api.call_args.args[2]["offset"], "page2")
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_dependency_self_reference_does_not_call_api(self):
        with patch.object(client, "_request") as api:
            with self.assertRaises(SystemExit):
                client.main(["add-dependency", "--gid", "2", "--depends-on", "2"])
        api.assert_not_called()

    def test_add_project_preserves_original_membership(self):
        task = {
            "gid": "1",
            "workspace": {"gid": "2"},
            "memberships": [
                {"project": {"gid": "3"}, "section": {"gid": "4"}},
            ],
        }
        target = {"project": {"gid": "5"}, "section": {"gid": "6"}}
        after = dict(task, memberships=task["memberships"] + [target])
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": task},
                {"data": {"workspace": {"gid": "2"}}},
                {"data": {"project": {"gid": "5"}}},
                {"data": {}},
                {"data": after},
            ],
        ) as api:
            result = client.main(
                ["add-project", "--gid", "1", "--project", "5", "--section", "6"]
            )
        self.assertEqual(result["data"], after)
        self.assertEqual(
            api.call_args_list[3].args,
            (
                "POST",
                "/tasks/1/addProject",
                None,
                {"project": "5", "section": "6"},
            ),
        )

    def test_add_project_with_existing_membership_does_not_reorder(self):
        task = {
            "workspace": {"gid": "2"},
            "memberships": [
                {"project": {"gid": "5"}, "section": {"gid": "7"}},
            ],
        }
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": task},
                {"data": {"workspace": {"gid": "2"}}},
                {"data": {"project": {"gid": "5"}}},
            ],
        ) as api:
            result = client.main(
                ["add-project", "--gid", "1", "--project", "5", "--section", "6"]
            )
        self.assertEqual(result["status"], "already_in_project_different_section")
        self.assertEqual(result["current_section_gid"], "7")
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_custom_field_numeric_values_and_null_are_verified(self):
        for value in (0, None):
            with self.subTest(value=value):
                task = {
                    "gid": "1",
                    "memberships": [{"project": {"gid": "2"}}],
                    "custom_fields": [{"gid": "3", "number_value": 8}],
                }
                settings = {
                    "data": [
                        {
                            "custom_field": {
                                "gid": "3",
                                "name": "見積",
                                "resource_subtype": "number",
                            }
                        }
                    ]
                }
                after = dict(task, custom_fields=[{"gid": "3", "number_value": value}])
                with patch.object(
                    client,
                    "_request",
                    side_effect=[
                        {"data": task},
                        settings,
                        {"data": {}},
                        {"data": after},
                    ],
                ) as api:
                    result = client.main(
                        [
                            "set-custom-field",
                            "--gid",
                            "1",
                            "--project",
                            "2",
                            "--field",
                            "3",
                            "--data",
                            client.json.dumps({"value": value}),
                        ]
                    )
                self.assertEqual(result["data"], after)
                self.assertEqual(
                    api.call_args_list[2].args,
                    (
                        "PUT",
                        "/tasks/1",
                        None,
                        {"custom_fields": {"3": value}},
                    ),
                )
                row = client.append_log.call_args.args[0]
                self.assertEqual(row["target"]["field_name"], "見積")
                self.assertEqual(row["diff"]["before"], {"number_value": 8})

    def test_custom_field_validation_blocks_write(self):
        for subtype, extra, value in (
            ("number", {}, True),
            ("number", {"is_formula_field": True}, 0),
            ("text", {"id_prefix": "ID"}, "value"),
            ("enum", {"enum_options": [{"gid": "4", "enabled": False}]}, "4"),
        ):
            with self.subTest(subtype=subtype, value=value):
                settings = {
                    "data": [
                        {
                            "custom_field": {
                                "gid": "3",
                                "name": "項目",
                                "resource_subtype": subtype,
                                **extra,
                            }
                        }
                    ]
                }
                task = {"memberships": [{"project": {"gid": "2"}}]}
                with patch.object(
                    client, "_request", side_effect=[{"data": task}, settings]
                ) as api:
                    with self.assertRaises((RuntimeError, ValueError)):
                        client.main(
                            [
                                "set-custom-field",
                                "--gid",
                                "1",
                                "--project",
                                "2",
                                "--field",
                                "3",
                                "--data",
                                client.json.dumps({"value": value}),
                            ]
                        )
                self.assertTrue(
                    all(call.args[0] == "GET" for call in api.call_args_list)
                )

    def test_custom_field_confirmation_failure_and_mismatch_do_not_repeat_put(self):
        task = {
            "gid": "1",
            "memberships": [{"project": {"gid": "2"}}],
            "custom_fields": [{"gid": "3", "enum_value": None}],
        }
        settings = {
            "data": [
                {
                    "custom_field": {
                        "gid": "3",
                        "name": "優先度",
                        "resource_subtype": "enum",
                        "enum_options": [{"gid": "4", "name": "高", "enabled": True}],
                    }
                }
            ]
        }
        for confirmation in (RuntimeError("network_failure"), {"data": task}):
            with self.subTest(confirmation=confirmation):
                with patch.object(
                    client,
                    "_request",
                    side_effect=[
                        {"data": task},
                        settings,
                        {"data": {}},
                        confirmation,
                    ],
                ) as api:
                    result = client.main(
                        [
                            "set-custom-field",
                            "--gid",
                            "1",
                            "--project",
                            "2",
                            "--field",
                            "3",
                            "--data",
                            '{"value":"4"}',
                        ]
                    )
                self.assertEqual(result["status"], "written_but_unverified")
                self.assertEqual(result["expected"], {"enum_value": "4"})
                self.assertEqual(
                    [call.args[0] for call in api.call_args_list],
                    ["GET", "GET", "PUT", "GET"],
                )

    def test_attachment_list_keeps_pagination_and_omits_view_url_from_log(self):
        attachment = {
            "gid": "2",
            "name": "spec.pdf",
            "view_url": "https://example.com/file",
        }
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"gid": "1"}},
                {"data": [attachment], "next_page": {"offset": "page2"}},
            ],
        ) as api:
            result = client.main(["attachments", "--gid", "1", "--max-items", "1"])
        self.assertEqual(
            result, {"data": [attachment], "has_more": True, "next_offset": "page2"}
        )
        self.assertEqual(api.call_args.args[1], "/attachments")
        self.assertEqual(api.call_args.args[2]["parent"], "1")
        self.assertNotIn(attachment["view_url"], str(client.append_log.call_args_list))

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
