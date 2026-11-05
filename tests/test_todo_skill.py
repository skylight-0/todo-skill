"""Exercise the skill against a real HTTP service and disposable SQLite data.

Build the ToolBox sync-service, then set TODO_SYNC_SERVICE_BINARY to its executable.
Run: python -m unittest discover -s tests -v
"""

import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/todo.py"
SPEC = importlib.util.spec_from_file_location("todo_skill", SCRIPT)
todo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(todo)
BINARY = Path(os.environ.get("TODO_SYNC_SERVICE_BINARY",
    str(ROOT / "target/todo-sync-service/debug" / ("todo-sync-service.exe" if os.name == "nt" else "todo-sync-service"))))
TOKEN = "isolated-todo-skill-test-" + uuid.uuid4().hex
DAY = 86_400_000


class TodoSkillTest(unittest.TestCase):
    def setUp(self):
        if not BINARY.is_file():
            self.fail("Build the service first; see this file's docstring")
        self.temporary = tempfile.TemporaryDirectory(prefix="todo-skill-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.address = f"http://127.0.0.1:{port}"
        self.service = todo.Service(self.address, TOKEN)
        self.config = self.folder / "config.json"
        self.config.write_text(json.dumps({"serverUrl": self.address, "token": TOKEN}), encoding="utf-8")
        self.state_dir = self.folder / "state"
        self.db = self.folder / "todo.sqlite"
        env = {**os.environ, "SYNC_BIND": f"127.0.0.1:{port}", "SYNC_TOKEN": TOKEN, "SYNC_DATABASE": str(self.db)}
        self.process = subprocess.Popen(
            [str(BINARY)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.addCleanup(self.stop_service)
        for _ in range(100):
            if self.process.poll() is not None:
                self.fail("Isolated sync service exited during startup")
            try:
                self.service.request("/healthz")
                break
            except todo.TodoError:
                time.sleep(0.03)
        else:
            self.fail("Isolated sync service did not start")

    def stop_service(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def call(self, *words):
        args = todo.parser().parse_args([
            "--config", str(self.config), "--state-dir", str(self.state_dir), *map(str, words),
        ])
        return todo.run(args)

    def entity(self, *words):
        return self.call(*words)["entities"][0]

    def pending(self):
        return self.call("pending")["pending"]

    def other_device(self, operations=()):
        return self.service.request("/api/v1/todo/sync", {
            "protocolVersion": 1, "clientId": "independent-device", "serverId": None,
            "cursor": 0, "operations": list(operations),
        })

    def test_metadata_partial_edits_and_date_conversion(self):
        self.assertTrue(self.call("doctor")["ok"])
        group = self.entity("group-add", "工作", "--color", "#3366CC")
        task = self.entity("add", "报告", "--description", "手机描述", "--priority", "urgent",
                           "--due", "2026-11-06", "--reminder", "on", "--group", group["id"])
        self.assertEqual(task["dueLocal"], "2026-11-06T23:59:00+08:00")
        before = task["fields"]
        # A distinct device changes a field the command does not edit.
        self.other_device([todo.operation("todo", task["id"], "patch", {"description": "来自手机的新描述"})])
        edited = self.entity("update", task["id"], "--text", "最终报告")
        self.assertEqual(edited["fields"]["description"], "来自手机的新描述")
        self.assertEqual(edited["fields"]["priority"], "urgent")
        self.assertEqual(edited["fields"]["groupId"], group["id"])
        self.assertEqual(edited["fields"]["reminderVersion"], before["reminderVersion"])
        self.assertEqual(group["fields"]["color"], 0xFF3366CC)
        # The other client sees the skill's edits through the unmodified API.
        mobile = next(e for e in self.other_device()["changes"] if e["id"] == task["id"])
        self.assertEqual(mobile["fields"], edited["fields"])
        no_due = self.entity("update", task["id"], "--due", "none")
        self.assertIsNone(no_due["fields"]["dueAt"])
        self.assertFalse(no_due["fields"]["reminderEnabled"])

    def test_resolution_rejects_ambiguity_and_missing_entities(self):
        first = self.entity("add", "同名")
        self.entity("add", "同名")
        with self.assertRaisesRegex(todo.TodoError, "不唯一"):
            self.call("delete", "同名")
        with self.assertRaisesRegex(todo.TodoError, "不存在"):
            self.call("update", "missing", "--text", "无目标")
        self.assertEqual(self.call("show", first["id"][:12])["id"], first["id"])
        self.assertEqual(self.call("list")["count"], 2)
        self.assertIsNone(self.pending())

    def test_trash_and_group_deletion_preserve_tasks(self):
        group = self.entity("group-add", "分组")
        task = self.entity("add", "可恢复", "--group", group["id"], "--description", "保留备注")
        self.call("done", task["id"])
        deleted = self.entity("delete", task["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.call("list")["count"], 0)
        self.assertEqual(self.call("list", "--trash")["count"], 1)
        self.call("group-delete", group["id"])
        self.assertEqual(self.call("groups", "--trash")["count"], 1)
        restored = self.entity("restore", task["id"])
        self.assertFalse(restored["deleted"])
        self.assertIsNone(restored["fields"]["groupId"])
        self.assertTrue(restored["fields"]["completed"])
        self.assertEqual(restored["fields"]["description"], "保留备注")
        self.call("group-restore", group["id"])
        self.assertIsNone(self.call("show", task["id"])["fields"]["groupId"])
        self.call("update", task["id"], "--group", group["id"])
        self.call("group-delete", group["id"])
        self.assertIsNone(self.call("show", task["id"])["fields"]["groupId"])

    def test_recurring_completion_reopen_and_terminal_count(self):
        due = todo.now_ms() - 60_000
        task = self.entity("add", "浇花", "--due", due, "--repeat", "day", "--interval", 3,
                           "--basis", "completion", "--count", 1, "--reminder", "on")
        before_version = task["fields"]["reminderVersion"]
        done = self.entity("done", task["id"])
        self.assertTrue(done["displayCompleted"])
        self.assertTrue(done["fields"]["completed"])
        self.assertEqual(len(done["fields"]["occurrences"]), 1)
        self.assertFalse(self.call("done", task["id"])["changed"])
        opened = self.entity("reopen", task["id"])
        self.assertFalse(opened["displayCompleted"])
        self.assertEqual(opened["fields"]["dueAt"], due)
        self.assertNotEqual(opened["fields"]["reminderVersion"], before_version)
        self.assertIsNone(opened["fields"]["recurrenceEndedAt"])

    def test_repeat_display_status_matches_flutter(self):
        task = self.entity("add", "每天", "--due", todo.now_ms() - 60_000, "--repeat", "day")
        done = self.entity("done", task["id"])
        self.assertFalse(done["fields"]["completed"])
        self.assertTrue(done["displayCompleted"])
        self.assertEqual(self.call("list", "--status", "completed")["count"], 1)
        self.assertEqual(self.call("list", "--status", "active")["count"], 0)
        self.assertFalse(self.call("done", task["id"])["changed"])
        self.call("reopen", task["id"])
        self.assertEqual(self.call("list", "--status", "active")["count"], 1)
        self.assertEqual(len(self.call("history", task["id"])["occurrences"]), 1)

    def test_archives_backfill_and_repeat_disable(self):
        anchor = todo.now_ms() - 4 * DAY
        task = self.entity("add", "历史", "--due", anchor, "--repeat", "day")
        rule = task["fields"]["recurrence"]
        changed = self.entity("update", task["id"], "--repeat", "week", "--due", todo.now_ms() + DAY)
        self.assertNotEqual(changed["fields"]["recurrence"]["id"], rule["id"])
        self.assertIn(rule["id"], changed["fields"]["recurrenceHistory"])
        due_before = changed["fields"]["dueAt"]
        recorded = self.entity("record", task["id"], "--status", "completed", "--scheduled", anchor,
                               "--rule-id", rule["id"], "--completed-at", anchor + 60_000)
        key = f"{rule['id']}:{anchor}"
        self.assertEqual(recorded["fields"]["dueAt"], due_before)
        self.assertEqual(recorded["fields"]["occurrences"][key]["status"], "completed")
        self.call("record", task["id"], "--status", "open", "--occurrence-id", key, "--replace")
        self.entity("update", task["id"], "--repeat", "none")
        self.assertEqual(self.call("history", task["id"])["occurrences"][key]["status"], "open")
        self.assertIsNone(self.call("show", task["id"])["fields"]["recurrence"])

    def test_completion_interval_skip_keeps_count(self):
        task = self.entity("add", "按完成后计时", "--due", todo.now_ms() - 60_000, "--repeat", "hour",
                           "--basis", "completion", "--count", 1)
        skipped = self.entity("skip", task["id"])
        self.assertFalse(skipped["fields"]["completed"])
        self.assertFalse(skipped["displayCompleted"])
        record = next(iter(skipped["fields"]["occurrences"].values()))
        self.assertEqual(record["status"], "skipped")
        self.assertGreater(skipped["fields"]["dueAt"], task["fields"]["dueAt"])

    def test_network_result_loss_retries_exact_create_and_blocks_writes(self):
        original = todo.Service.request
        observed = []

        def commit_then_lose_response(client, path, body=None):
            result = original(client, path, body)
            if body and body.get("operations"):
                observed.append(body)
                raise todo.TodoError("simulated response loss")
            return result

        with mock.patch.object(todo.Service, "request", commit_then_lose_response):
            with self.assertRaisesRegex(todo.TodoError, "response loss"):
                self.call("add", "只创建一次")
        pending = self.pending()
        self.assertEqual(pending["operations"], observed[0]["operations"])
        with self.assertRaisesRegex(todo.TodoError, "未确认"):
            self.call("add", "另一项")
        # Read commands see the commit, without silently replaying anything.
        self.assertEqual(self.call("list")["count"], 1)
        retried = self.call("retry")
        self.assertEqual(retried["acknowledged"], [pending["operations"][0]["opId"]])
        self.assertEqual(self.call("list")["count"], 1)
        self.assertIsNone(self.pending())

    def test_lost_recurring_completion_does_not_advance_twice(self):
        task = self.entity("add", "重试完成", "--due", todo.now_ms() - 60_000, "--repeat", "day", "--basis", "completion")
        original = todo.Service.request

        def lose(client, path, body=None):
            result = original(client, path, body)
            if body and body.get("operations"):
                raise todo.TodoError("lost")
            return result

        with mock.patch.object(todo.Service, "request", lose):
            with self.assertRaises(todo.TodoError):
                self.call("done", task["id"])
        due = self.call("show", task["id"])["fields"]["dueAt"]
        retried = self.entity("retry")
        self.assertEqual(retried["fields"]["dueAt"], due)
        self.assertEqual(len(retried["fields"]["occurrences"]), 1)

    def test_server_identity_change_retains_pending(self):
        original = todo.Service.request

        def fail_write(client, path, body=None):
            if body and body.get("operations"):
                raise todo.TodoError("offline")
            return original(client, path, body)

        with mock.patch.object(todo.Service, "request", fail_write):
            with self.assertRaises(todo.TodoError):
                self.call("add", "不能跨数据库发送")
        before = self.pending()
        # Simulate a replacement server at the same URL, without touching a live DB.
        changed = {**self.service.snapshot("observer"), "serverId": "replaced-server"}
        with mock.patch.object(todo.Service, "snapshot", return_value=changed):
            with self.assertRaisesRegex(todo.TodoError, "serverId"):
                self.call("retry")
        self.assertEqual(self.pending(), before)
        self.assertEqual(self.call("list")["count"], 0)

    def test_server_rejection_rolls_back_and_clears_pending(self):
        task = self.entity("add", "无效补记", "--due", todo.now_ms() - 60_000, "--repeat", "day")
        with self.assertRaisesRegex(todo.TodoError, "HTTP 400"):
            self.call("record", task["id"], "--status", "completed", "--scheduled", task["fields"]["dueAt"] + 1234)
        self.assertIsNone(self.pending())
        self.assertEqual(self.call("history", task["id"])["occurrences"], {})

    def test_filters_and_validation_before_writes(self):
        tz = todo.parse_timezone("+08:00")
        today = todo.datetime.now(tz).date().isoformat()
        self.entity("add", "当天", "--due", today, "--description", "FILTER")
        self.entity("add", "没有日期")
        self.assertEqual(self.call("list", "--date", "today")["count"], 1)
        self.assertEqual(self.call("list", "--query", "filter")["count"], 1)
        self.assertEqual(self.call("list", "--date", "unscheduled")["count"], 1)
        for args in (("add", "bad", "--reminder", "on"),
                     ("add", "bad", "--repeat", "day"),
                     ("add", "bad", "--interval", 3),
                     ("add", "bad", "--position", "nan"),
                     ("add", "bad", "--due", today, "--repeat", "day", "--interval", 0),
                     ("list", "--date", "invalid")):
            with self.subTest(args=args), self.assertRaises(todo.TodoError):
                self.call(*args)
        self.assertEqual(self.call("list")["count"], 2)
        self.assertIsNone(self.pending())

    def test_toolbox_config_read_only_and_secret_redaction(self):
        client_db = self.folder / "clipboard.sqlite"
        with closing(sqlite3.connect(client_db)) as connection:
            connection.execute("CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO app_settings VALUES (?, ?)", ("todo_sync_config_v1", json.dumps({
                "enabled": True, "serverUrl": self.address, "token": TOKEN,
            })))
            connection.commit()
        before = client_db.read_bytes()
        clean_env = {key: value for key, value in os.environ.items()
                     if not key.startswith("TODO_") and key != "SYNC_TOKEN"}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            args = todo.parser().parse_args(["--toolbox-db", str(client_db), "--state-dir", str(self.state_dir), "doctor"])
            result = todo.run(args)
        self.assertTrue(result["ok"])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(client_db.read_bytes(), before)
        state_json = next(self.state_dir.rglob("state.json")).read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, state_json)
        self.config.write_text(json.dumps({"serverUrl": self.address, "token": "wrong-token-with-valid-length"}), encoding="utf-8")
        with self.assertRaisesRegex(todo.TodoError, "HTTP 401") as caught:
            self.call("doctor")
        self.assertNotIn("wrong-token-with-valid-length", str(caught.exception))

    def test_capability_gate_allows_normal_tasks_but_blocks_repeat(self):
        original = todo.Service.request

        def legacy_health(client, path, body=None):
            return {"status": "ok", "protocolVersion": 1} if path == "/healthz" else original(client, path, body)

        with mock.patch.object(todo.Service, "request", legacy_health):
            self.entity("add", "普通任务")
            with self.assertRaisesRegex(todo.TodoError, "recurrence-v1"):
                self.call("add", "重复任务", "--due", todo.now_ms(), "--repeat", "day")
        self.assertEqual(self.call("list")["count"], 1)
        self.assertIsNone(self.pending())

    def test_invalid_connection_and_clock_values_stop_before_request(self):
        for address in ("http://127.0.0.1:0", "http://user:secret@localhost", "https://host/?token=secret",
                        "https://host/api/v1/todo/sync", "not-a-url"):
            with self.subTest(address=address):
                self.config.write_text(json.dumps({"serverUrl": address, "token": TOKEN}), encoding="utf-8")
                with mock.patch.object(todo.Service, "request") as request:
                    with self.assertRaises(todo.TodoError):
                        self.call("doctor")
                    request.assert_not_called()
        for offset in ("+14:01", "-15:00", "Asia/Taipei"):
            with self.subTest(offset=offset), self.assertRaises(todo.TodoError):
                todo.parse_timezone(offset)
        tz = todo.parse_timezone("-05:00")
        self.assertEqual(todo.parse_time("2026-11-06T10:00:00", tz),
                         todo.parse_time("2026-11-06T15:00:00Z", tz))
        with self.assertRaises(todo.TodoError):
            todo.parse_time("2026-02-30", tz)

    def test_snapshot_return_handles_more_than_one_delta_page(self):
        # Seed enough actual entities to verify a reset snapshot is complete.
        for batch in range(3):
            self.other_device([todo.operation("todo", f"bulk-{batch}-{i}", "create", {"text": "批量"}) for i in range(200)])
        task = self.entity("add", "第 601 项")
        self.assertEqual(self.call("list")["count"], 601)
        self.assertEqual(task["fields"]["text"], "第 601 项")
        self.assertEqual(self.call("show", task["id"])["id"], task["id"])

    def test_process_lock_and_subprocess_entrypoint(self):
        with todo.State(self.state_dir, self.address).locked():
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--config", str(self.config),
                "--state-dir", str(self.state_dir), "list",
            ], capture_output=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn("已有", json.loads(result.stderr)["error"])
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--config", str(self.config),
            "--state-dir", str(self.state_dir), "add", "子进程任务",
        ], capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["entities"][0]["fields"]["text"], "子进程任务")


if __name__ == "__main__":
    unittest.main(verbosity=2)
