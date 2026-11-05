#!/usr/bin/env python3
"""Operate the shared Todo service without editing either client's local data."""

import argparse
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class TodoError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise TodoError(f"无法读取 JSON 文件 {path}：{error}") from error


def load_config(args):
    config_path = args.config or os.environ.get("TODO_SYNC_CONFIG")
    source = None
    if config_path:
        config = read_json(config_path)
        source = str(config_path)
    elif os.environ.get("TODO_SYNC_URL") or os.environ.get("TODO_SYNC_TOKEN") or os.environ.get("SYNC_TOKEN"):
        config = {
            "serverUrl": os.environ.get("TODO_SYNC_URL", "http://127.0.0.1:8787"),
            "token": os.environ.get("TODO_SYNC_TOKEN") or os.environ.get("SYNC_TOKEN", ""),
        }
        if os.environ.get("TODO_SYNC_TOKEN") and not os.environ.get("TODO_SYNC_URL"):
            raise TodoError("请同时设置 TODO_SYNC_URL 和 TODO_SYNC_TOKEN")
        source = "environment"
    else:
        db = args.toolbox_db or os.environ.get("TODO_TOOLBOX_DB")
        if not db:
            roaming = os.environ.get("APPDATA")
            if not roaming:
                raise TodoError("未找到同步配置；请设置 TODO_SYNC_CONFIG 或 TODO_SYNC_URL / TODO_SYNC_TOKEN")
            db = Path(roaming) / "com.zhao.toolbox" / "clipboard.sqlite"
        db = Path(db).expanduser().resolve()
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                row = connection.execute(
                    "SELECT value FROM app_settings WHERE key=?", ("todo_sync_config_v1",)
                ).fetchone()
            config = json.loads(row[0]) if row else {}
        except (sqlite3.Error, ValueError) as error:
            raise TodoError("无法只读加载 ToolBox 同步配置；请检查数据库路径或使用 --config") from error
        if not isinstance(config, dict) or not config.get("enabled"):
            raise TodoError("ToolBox 尚未启用同步；请先在待办的云端同步中接入原服务，或使用 --config")
        source = str(db)
    if not isinstance(config, dict):
        raise TodoError("同步配置须为包含 serverUrl 和 token 的 JSON 对象")
    address = args.url or config.get("serverUrl", "")
    token = config.get("token", "")
    if not isinstance(address, str) or re.search(r"\s", address):
        raise TodoError("同步地址须为 HTTP 或 HTTPS 根地址")
    try:
        parsed = urlsplit(address)
        port = parsed.port
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.username and not parsed.password and not parsed.query
                 and not parsed.fragment and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid or "/api/v1/todo" in parsed.path:
        raise TodoError("同步地址须为 HTTP 或 HTTPS 根地址，不含凭据、查询参数或 /api/v1/todo")
    if (not isinstance(token, str) or len(token) < 24 or not token.isascii()
            or re.search(r"[\x00-\x20\x7f]", token)):
        raise TodoError("个人密钥须至少 24 个 ASCII 字符，且不含空格或控制字符")
    return address.rstrip("/"), token, source


def parse_timezone(value):
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match:
        raise TodoError("时区格式为 +08:00 或 -05:00")
    hours, minutes = int(match[2]), int(match[3])
    if minutes > 59 or hours > 14 or (hours == 14 and minutes):
        raise TodoError("时区偏移须在 -14:00 到 +14:00 之间")
    offset = (hours * 60 + minutes) * (1 if match[1] == "+" else -1)
    return timezone(timedelta(minutes=offset))


def parse_time(value, tz):
    if value == "none":
        return None
    try:
        if re.fullmatch(r"\d+", value):
            result = int(value)
        else:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                instant = datetime.combine(date.fromisoformat(value), time(23, 59), tz)
            else:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=tz)
            result = int(instant.timestamp() * 1000)
        if not 0 < result <= 32_503_680_000_000:
            raise ValueError()
        return result
    except (ValueError, OverflowError, OSError) as error:
        raise TodoError(f"无效时间 {value!r}；请使用 ISO 8601、日期或 Unix 毫秒") from error


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Service:
    def __init__(self, address, token):
        self.address, self.token = address, token
        self.opener = build_opener(NoRedirect())

    def request(self, path, body=None):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self.token}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(self.address + path, payload, headers), timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.load(error).get("error", error.reason)
            except (ValueError, AttributeError):
                message = error.reason
            message = str(message).replace(self.token, "[redacted]")
            raise TodoError(f"HTTP {error.code}：{message}", status=error.code) from error
        except (URLError, OSError, HTTPException) as error:
            raise TodoError("无法连接同步服务或响应中断；检查网络、地址和服务状态") from error
        except ValueError as error:
            raise TodoError("同步服务未返回有效 JSON；请求可能已提交，请查看 pending") from error

    def snapshot(self, client_id):
        result = self.request("/api/v1/todo/sync", {
            "protocolVersion": 1, "clientId": client_id, "serverId": None,
            "cursor": 0, "operations": [],
        })
        if not isinstance(result, dict) or not result.get("reset") or result.get("hasMore"):
            raise TodoError("服务未返回完整快照，已停止操作")
        if (not isinstance(result.get("serverId"), str)
                or not isinstance(result.get("cursor"), int)
                or not isinstance(result.get("changes"), list)):
            raise TodoError("同步快照结构无效，已停止操作")
        return result

    def require_recurrence(self):
        health = self.request("/healthz")
        if "recurrence-v1" not in health.get("capabilities", []):
            raise TodoError("服务尚未支持 recurrence-v1；请先更新并部署 sync-service")


class State:
    def __init__(self, root, address):
        if root is None:
            local = os.environ.get("LOCALAPPDATA")
            root = Path(local) / "Codex" / "todo-skill" if local else Path.home() / ".local/state/codex/todo-skill"
        self.folder = Path(root) / hashlib.sha256(address.encode()).hexdigest()[:24]
        self.path = self.folder / "state.json"
        self.address = address

    @contextmanager
    def locked(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / "lock").open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise TodoError("同一服务已有 Todo skill 命令运行，请稍后重试") from error
            try:
                if self.path.exists():
                    self.data = read_json(self.path)
                    if (not isinstance(self.data, dict) or self.data.get("version") != 1
                            or self.data.get("serverUrl") != self.address
                            or not isinstance(self.data.get("clientId"), str)):
                        raise TodoError("skill 状态文件无效，请保留原文件后检查，避免丢失待确认操作")
                else:
                    self.data = {"version": 1, "serverUrl": self.address,
                                 "clientId": str(uuid.uuid4()), "pending": None}
                    self.save()
                yield self
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self):
        descriptor, name = tempfile.mkstemp(prefix=".state-", dir=self.folder)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def resolve(entities, kind, reference, deleted=False):
    candidates = [e for e in entities if e["kind"] == kind and e["deleted"] == deleted]
    exact = [e for e in candidates if e["id"] == reference]
    if exact:
        return exact[0]
    key = "text" if kind == "todo" else "name"
    matches = [e for e in candidates if e["id"].startswith(reference) or e["fields"].get(key) == reference]
    if len(matches) != 1:
        detail = [{"id": e["id"], key: e["fields"].get(key)} for e in matches]
        raise TodoError("目标不存在" if not matches else "目标不唯一，请使用完整 ID：" + json.dumps(detail, ensure_ascii=False))
    return matches[0]


def group_id(entities, reference):
    return None if reference == "root" else resolve(entities, "group", reference)["id"]


def completed_record(fields, now):
    rule = fields.get("recurrence")
    if not rule or (not fields["completed"] and (fields.get("dueAt") is None or now >= fields["dueAt"])):
        return None
    record = fields.get("occurrences", {}).get(fields.get("advancedFrom"))
    if record and record["status"] == "completed" and record["rule"]["id"] == rule["id"]:
        return record
    return None


def display_completed(fields, now):
    return bool(fields["completed"] or completed_record(fields, now))


def present(entity, tz, now):
    result = dict(entity)
    if entity["kind"] == "todo":
        fields = entity["fields"]
        due = fields.get("dueAt")
        result["dueLocal"] = datetime.fromtimestamp(due / 1000, tz).isoformat() if due else None
        result["displayCompleted"] = display_completed(fields, now)
    return result


def matches_date(fields, value, tz, now):
    due = fields.get("dueAt")
    if value == "all":
        return True
    if value == "unscheduled":
        return due is None
    if due is None:
        return False
    if value == "overdue":
        return due < now and not display_completed(fields, now)
    today = datetime.fromtimestamp(now / 1000, tz).date()
    day = datetime.fromtimestamp(due / 1000, tz).date()
    if value == "today":
        return day == today
    if value == "tomorrow":
        return day == today + timedelta(days=1)
    if value == "week":
        return today <= day < today + timedelta(days=7)
    try:
        return day == date.fromisoformat(value)
    except ValueError as error:
        raise TodoError("日期筛选无效，请使用 today、tomorrow、week、overdue、unscheduled 或 YYYY-MM-DD") from error


def position(entities, kind, parent=None):
    peers = [e["fields"].get("position", 0) for e in entities if not e["deleted"]
             and ((e["kind"] == "todo" and e["fields"].get("groupId") == parent)
                  or (parent is None and e["kind"] == "group"))]
    value = min(peers, default=1024) - 1024
    if abs(value) > 1e15:
        raise TodoError("排序位置已达上限，请显式指定 --position")
    return value


def on_off(value):
    return value == "on"


def bounded_text(value, limit, label, empty=False):
    if (not empty and not value.strip()) or len(value) > limit:
        raise TodoError(f"{label}须为{'0' if empty else '1'}–{limit} 个字符")
    return value


def color(value):
    try:
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            result = int(value[1:], 16) | 0xFF000000
        elif re.fullmatch(r"#[0-9a-fA-F]{8}", value):
            result = int(value[1:], 16)
        else:
            result = int(value)
        if not 0 <= result <= 0xFFFFFFFF:
            raise ValueError()
        return result
    except ValueError as error:
        raise TodoError("颜色须为 #RRGGBB、#AARRGGBB 或 32 位无符号整数") from error


def task_patch(args, entities, current, tz, now):
    patch = {}
    if args.text is not None:
        patch["text"] = bounded_text(args.text, 2000, "任务内容")
    if args.description is not None:
        patch["description"] = bounded_text(args.description, 20000, "描述", empty=True)
    if args.priority is not None:
        patch["priority"] = args.priority
    if args.due is not None:
        patch["dueAt"] = parse_time(args.due, tz)
        if current.get("recurrence") and args.repeat is None:
            raise TodoError("修改重复任务时间需同时传 --repeat，生成新规则并归档旧计划")
    if args.reminder is not None:
        patch["reminderEnabled"] = on_off(args.reminder)
    if args.group is not None:
        patch["groupId"] = group_id(entities, args.group)
    if args.position is not None:
        if not math.isfinite(args.position) or abs(args.position) > 1e15:
            raise TodoError("position 须为绝对值不超过 1e15 的有限数值")
        patch["position"] = args.position
    modifiers = (args.interval, args.basis, args.end, args.count)
    if args.repeat is None and any(v is not None for v in modifiers):
        raise TodoError("重复选项需要同时指定 --repeat")
    if args.repeat is not None:
        if args.repeat == "none":
            if any(v is not None for v in modifiers):
                raise TodoError("--repeat none 不接受重复间隔或结束选项")
            patch["recurrence"] = None
        else:
            anchor = patch.get("dueAt", current.get("dueAt"))
            if not anchor:
                raise TodoError("重复任务需要首次截止时间 --due")
            interval, count = args.interval if args.interval is not None else 1, args.count
            if not 1 <= interval <= 999 or (count is not None and not 1 <= count <= 9999):
                raise TodoError("重复间隔须为 1–999，完成次数须为 1–9999")
            end = parse_time(args.end, tz) if args.end is not None else None
            if end is not None and end < anchor:
                raise TodoError("重复结束时间不能早于首次时间")
            patch["recurrence"] = {
                "id": str(uuid.uuid4()), "interval": interval, "unit": args.repeat,
                "basis": args.basis or "schedule", "anchorAt": anchor,
                "timezoneOffsetMinutes": int(tz.utcoffset(None).total_seconds() / 60),
                "endAt": end, "maxOccurrences": count,
            }
        patch["recurrenceChangedAt"] = now
    effective_due = patch.get("dueAt", current.get("dueAt"))
    if patch.get("recurrence"):
        effective_due = patch["recurrence"]["anchorAt"]
    if patch.get("reminderEnabled") and not effective_due:
        raise TodoError("开启提醒需要设置截止时间 --due")
    return patch


def occurrence_patch(args, fields, tz, now):
    records = fields.get("occurrences", {})
    occurrence_id = getattr(args, "occurrence_id", None)
    record = records.get(occurrence_id) if occurrence_id else None
    if args.command == "reopen":
        record = completed_record(fields, now)
        if record is None:
            return None
        occurrence_id = fields["advancedFrom"]
    rule_id = getattr(args, "rule_id", None)
    scheduled_arg = getattr(args, "scheduled", None)
    if record:
        rule, scheduled = record["rule"], record["scheduledAt"]
        if (rule_id and rule_id != rule["id"]) or (scheduled_arg and parse_time(scheduled_arg, tz) != scheduled):
            raise TodoError("指定规则或计划时间与原记录不一致")
    else:
        rule = fields.get("recurrence")
        if rule_id and (not rule or rule["id"] != rule_id):
            rule = fields.get("recurrenceHistory", {}).get(rule_id, {}).get("rule")
        if not rule:
            raise TodoError("找不到对应重复规则，请先查看 history")
        scheduled = parse_time(scheduled_arg, tz) if scheduled_arg else fields.get("dueAt")
        if scheduled is None:
            raise TodoError("需要有效的计划时间 --scheduled")
        if rule != fields.get("recurrence") and not scheduled_arg:
            raise TodoError("操作归档规则需提供原计划时间 --scheduled")
        if not occurrence_id:
            occurrence_id = (fields.get("currentOccurrenceId")
                             if rule == fields.get("recurrence") and scheduled == fields.get("dueAt") else None)
            occurrence_id = occurrence_id or f"{rule['id']}:{scheduled}"
    status = {"done": "completed", "skip": "skipped", "reopen": "open"}.get(args.command)
    status = status or args.status
    completed_at = None
    if status == "completed":
        completed_arg = getattr(args, "completed_at", None)
        completed_at = parse_time(completed_arg, tz) if completed_arg else now
        if completed_at is None or completed_at > now:
            raise TodoError("完成时间须为不晚于当前时刻的有效时间")
    elif getattr(args, "completed_at", None):
        raise TodoError("只有 completed 记录接受 --completed-at")
    return {
        "occurrence": {"id": occurrence_id, "scheduledAt": scheduled, "status": status,
                       "completedAt": completed_at, "recordedAt": now, "rule": rule},
        "replace": args.command == "reopen" or getattr(args, "replace", False),
    }


def operation(kind, entity_id, action, patch):
    return {"opId": str(uuid.uuid4()), "kind": kind, "id": entity_id, "action": action, "patch": patch}


def build_operation(args, entities, tz, now):
    command = args.command
    if command == "add":
        patch = task_patch(args, entities, {}, tz, now)
        patch.setdefault("position", position(entities, "todo", patch.get("groupId")))
        return operation("todo", str(uuid.uuid4()), "create", patch)
    if command.startswith("group-"):
        if command == "group-add":
            entity = None
            patch = {"name": bounded_text(args.name, 80, "分组名称")}
        else:
            entity = resolve(entities, "group", args.reference, command == "group-restore")
            patch = {}
            if command == "group-update" and args.name is not None:
                patch["name"] = bounded_text(args.name, 80, "分组名称")
        for key in ("color", "collapsed", "position"):
            value = getattr(args, key, None)
            if value is not None:
                patch[key] = color(value) if key == "color" else on_off(value) if key == "collapsed" else value
        if "position" in patch and (not math.isfinite(patch["position"]) or abs(patch["position"]) > 1e15):
            raise TodoError("position 须为绝对值不超过 1e15 的有限数值")
        if command == "group-add":
            patch.setdefault("position", position(entities, "group"))
        action = {"group-add": "create", "group-update": "patch", "group-delete": "delete", "group-restore": "restore"}[command]
        if action == "patch" and not patch:
            return None
        return operation("group", entity["id"] if entity else str(uuid.uuid4()), action, patch)
    entity = resolve(entities, "todo", args.reference, command == "restore")
    fields = entity["fields"]
    if command == "update":
        patch = task_patch(args, entities, fields, tz, now)
        return operation("todo", entity["id"], "patch", patch) if patch else None
    if command in ("delete", "restore"):
        patch = {}
        if command == "restore" and args.group is not None:
            patch["groupId"] = group_id(entities, args.group)
        return operation("todo", entity["id"], command, patch)
    if command in ("done", "reopen", "skip", "record"):
        if command == "done" and display_completed(fields, now):
            return None
        if command == "reopen" and not display_completed(fields, now):
            return None
        if command == "skip" and display_completed(fields, now):
            raise TodoError("当前周期已完成；要跳过下一次请通过 record 明确指定计划")
        if fields.get("recurrence") or command == "record":
            patch = occurrence_patch(args, fields, tz, now)
            return operation("todo", entity["id"], "recordOccurrence", patch) if patch else None
        if command == "skip":
            raise TodoError("只有重复任务支持跳过一次")
        return operation("todo", entity["id"], "patch", {"completed": command == "done"})
    raise TodoError(f"未知写操作 {command}")


def commit(service, state, snapshot, op=None):
    if op is not None:
        state.data["pending"] = {"serverId": snapshot["serverId"], "operations": [op]}
        state.save()
    pending = state.data["pending"]
    if pending["serverId"] != snapshot["serverId"]:
        raise TodoError("服务 serverId 已改变，保留 pending；请先核对是否更换或恢复了数据库")
    try:
        response = service.request("/api/v1/todo/sync", {
            "protocolVersion": 1, "clientId": state.data["clientId"],
            "serverId": pending["serverId"], "cursor": snapshot["cursor"],
            "operations": pending["operations"],
        })
    except TodoError as error:
        # These responses reject the complete transaction. Keep conflicts and
        # ambiguous network/5xx results for inspection and exact replay.
        if error.status in (400, 401, 403, 404, 405, 413, 415, 422):
            state.data["pending"] = None
            state.save()
        raise
    expected = {item["opId"] for item in pending["operations"]}
    if (not isinstance(response, dict) or response.get("serverId") != pending["serverId"]
            or response.get("reset") or not expected.issubset(set(response.get("acknowledged", [])))):
        raise TodoError("服务未确认原操作或数据库已改变，保留 pending；请核对后 retry")
    # A reset snapshot contains the committed entities even when deltas exceed
    # one page, so a large concurrent update cannot hide the result.
    latest = service.snapshot(state.data["clientId"])
    if latest["serverId"] != pending["serverId"]:
        raise TodoError("提交后的 serverId 已改变，保留 pending；请人工核对")
    targets = {(item["kind"], item["id"]) for item in pending["operations"]}
    result = [e for e in latest["changes"] if (e["kind"], e["id"]) in targets]
    if len(result) != len(targets):
        raise TodoError("提交后的快照缺少目标，保留 pending；请核对后 retry")
    state.data["pending"] = None
    state.save()
    return {"ok": True, "acknowledged": sorted(expected), "entities": result}


READ_COMMANDS = {"doctor", "list", "show", "groups", "history", "pending"}


def run(args):
    address, token, source = load_config(args)
    tz = parse_timezone(args.timezone)
    service = Service(address, token)
    with State(args.state_dir, address).locked() as state:
        if args.command == "pending":
            return {"serverUrl": address, "pending": state.data["pending"]}
        if args.command not in READ_COMMANDS and args.command != "retry" and state.data["pending"]:
            raise TodoError("存在未确认操作；先运行 pending 查看，再用 retry 复用原操作 ID")
        if args.command == "retry" and not state.data["pending"]:
            return {"ok": True, "changed": False, "message": "没有待重试操作"}
        health = service.request("/healthz") if args.command == "doctor" else None
        snapshot = service.snapshot(state.data["clientId"])
        entities, now = snapshot["changes"], now_ms()
        if args.command == "doctor":
            return {"ok": True, "serverUrl": address, "configSource": source,
                    "serverId": snapshot["serverId"], "health": health,
                    "tasks": sum(e["kind"] == "todo" and not e["deleted"] for e in entities),
                    "groups": sum(e["kind"] == "group" and not e["deleted"] for e in entities),
                    "hasPending": bool(state.data["pending"]), "timezone": args.timezone}
        if args.command in ("list", "groups"):
            kind = "todo" if args.command == "list" else "group"
            selected = [e for e in entities if e["kind"] == kind and e["deleted"] == args.trash]
            if kind == "todo":
                # Validate even when the snapshot or candidate list is empty.
                if args.date not in ("all", "today", "tomorrow", "week", "overdue", "unscheduled"):
                    try:
                        date.fromisoformat(args.date)
                    except ValueError as error:
                        raise TodoError("日期筛选须为 YYYY-MM-DD 或内置筛选名称") from error
                parent = group_id(entities, args.group) if args.group is not None else None
                selected = [e for e in selected
                            if (args.group is None or e["fields"].get("groupId") == parent)
                            and (args.status == "all" or display_completed(e["fields"], now) == (args.status == "completed"))
                            and matches_date(e["fields"], args.date, tz, now)
                            and args.query.casefold() in (e["fields"]["text"] + "\n" + e["fields"].get("description", "")).casefold()]
            selected.sort(key=lambda e: (e["fields"].get("position", 0), e["id"]))
            return {"count": len(selected), "entities": [present(e, tz, now) for e in selected]}
        if args.command in ("show", "history"):
            entity = resolve(entities, "todo", args.reference, getattr(args, "trash", False))
            if args.command == "show":
                return present(entity, tz, now)
            keys = ("recurrence", "recurrenceHistory", "occurrences", "currentOccurrenceId", "advancedFrom", "recurrenceEndedAt", "dueAt")
            return {"id": entity["id"], **{key: entity["fields"].get(key) for key in keys}}
        if args.command == "retry":
            result = commit(service, state, snapshot)
        else:
            op = build_operation(args, entities, tz, now)
            if op is None:
                return {"ok": True, "changed": False, "message": "当前状态已满足要求"}
            if "recurrence" in op["patch"] or op["action"] == "recordOccurrence":
                service.require_recurrence()
            result = commit(service, state, snapshot, op)
        result["entities"] = [present(e, tz, now_ms()) for e in result["entities"]]
        return result


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", help="私有 JSON 同步配置")
    root.add_argument("--url", help="覆盖已选配置的服务根地址")
    root.add_argument("--toolbox-db", help="只读加载指定 ToolBox clipboard.sqlite 的同步配置")
    root.add_argument("--state-dir", default=os.environ.get("TODO_SKILL_STATE_DIR"), help="skill 的客户端 ID 与待确认操作存储目录")
    root.add_argument("--timezone", default=os.environ.get("TODO_TIMEZONE", "+08:00"), help="日期解释的固定偏移，默认 +08:00")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("doctor", "pending", "retry"):
        commands.add_parser(name)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=("all", "active", "completed"), default="all")
    listing.add_argument("--date", default="all")
    listing.add_argument("--group")
    listing.add_argument("--query", default="")
    listing.add_argument("--trash", action="store_true")
    commands.add_parser("groups").add_argument("--trash", action="store_true")
    for name in ("show", "history", "done", "reopen", "skip", "delete", "restore", "group-delete", "group-restore"):
        command = commands.add_parser(name)
        command.add_argument("reference", help="完整 ID、唯一 ID 前缀或精确名称")
        if name in ("show", "history"):
            command.add_argument("--trash", action="store_true")
        if name == "restore":
            command.add_argument("--group")
    for name in ("add", "update"):
        command = commands.add_parser(name)
        command.add_argument("text" if name == "add" else "reference")
        if name == "update":
            command.add_argument("--text")
        command.add_argument("--description")
        command.add_argument("--priority", choices=("low", "normal", "high", "urgent"))
        command.add_argument("--due", help="ISO 8601、日期、Unix 毫秒或 none")
        command.add_argument("--reminder", choices=("on", "off"))
        command.add_argument("--group", help="分组 ID、名称或 root")
        command.add_argument("--position", type=float)
        command.add_argument("--repeat", choices=("hour", "day", "week", "month", "none"))
        command.add_argument("--interval", type=int)
        command.add_argument("--basis", choices=("schedule", "completion"))
        command.add_argument("--end")
        command.add_argument("--count", type=int)
    for name in ("group-add", "group-update"):
        command = commands.add_parser(name)
        command.add_argument("name" if name == "group-add" else "reference")
        if name == "group-update":
            command.add_argument("--name")
        command.add_argument("--color")
        command.add_argument("--collapsed", choices=("on", "off"))
        command.add_argument("--position", type=float)
    record = commands.add_parser("record")
    record.add_argument("reference")
    record.add_argument("--status", required=True, choices=("completed", "skipped", "open"))
    record.add_argument("--occurrence-id")
    record.add_argument("--scheduled")
    record.add_argument("--rule-id")
    record.add_argument("--completed-at")
    record.add_argument("--replace", action="store_true")
    return root


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = run(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (TodoError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
