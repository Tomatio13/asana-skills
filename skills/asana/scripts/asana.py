#!/usr/bin/env python3
"""Small dependency-free Asana client."""

import argparse
from contextvars import ContextVar
import uuid
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://app.asana.com/api/1.0"
RUN_ID = ContextVar("asana_run_id", default=None)
AUDIT = ContextVar("asana_audit", default=None)
ACTIONS = {
    "me": "get_current_user",
    "workspaces": "list_workspaces",
    "users": "list_users",
    "resolve-user": "resolve_user",
    "projects": "list_projects",
    "sections": "list_sections",
    "tasks": "list_tasks",
    "task": "get_task",
    "comments": "list_comments",
    "search": "search_tasks",
    "create": "create_task",
    "update": "update_task",
    "comment": "add_comment",
    "move": "move_task",
    "subtasks": "list_subtasks",
    "create-subtask": "create_subtask",
}


def api_metadata(response):
    headers = getattr(response, "headers", {})
    return {
        "http_status": getattr(response, "status", getattr(response, "code", None)),
        "asana_request_id": headers.get("X-Asana-Request-Id")
        or headers.get("X-Request-Id"),
        "retry_after": headers.get("Retry-After"),
    }


def operation_summary(context, outcome):
    state = AUDIT.get() or {}
    calls = state.get("calls", [])
    writes = [c for c in calls if c["method"] != "GET"]
    primary = writes[-1] if writes else (calls[-1] if calls else {})
    task = {}
    # Task snapshots, not comment/story response IDs, identify the operation target.
    for call in calls:
        observed = call.get("observed")
        if isinstance(observed, dict) and (
            (call["path"].startswith("/tasks/") and call["path"].count("/") == 2)
            or call["path"] == "/tasks"
            or call["path"].endswith("/subtasks")
            and call["method"] == "POST"
        ):
            task = dict(task, **observed)
    target = {
        "workspace_gid": context.get("workspace"),
        "project_gid": context.get("project"),
        "task_gid": task.get("gid") or context.get("gid"),
        "task_name": task.get("name"),
        "permalink_url": task.get("permalink_url"),
    }
    if isinstance(task.get("workspace"), dict):
        target["workspace_gid"] = task["workspace"].get("gid")
    projects = task.get("projects") or [
        m["project"] for m in task.get("memberships", []) if m.get("project")
    ]
    if projects:
        target["project_gid"] = projects[0].get("gid")
        target["project_gids"] = [p["gid"] for p in projects if "gid" in p]
    if isinstance(task.get("parent"), dict):
        target["parent_task_gid"] = task["parent"].get("gid")
    if context["operation"] == "resolve-user":
        resolved = state.get("resolved_user") or {}
        target.update(
            user_gid=resolved.get("gid"),
            user_name=resolved.get("name"),
            user_email=resolved.get("email") or state.get("lookup_email"),
            membership_verified=resolved.get("membership_verified", False),
        )
    requested = primary.get("requested") or {}
    before = primary.get("before") or {}
    after = {}
    if writes and primary.get("outcome") == "success":
        if context["operation"] == "move":
            requested = {
                "memberships": {"destination_section_gid": context.get("section")}
            }
        elif context["operation"] == "comment":
            after = {
                k: v
                for k, v in (primary.get("observed") or {}).items()
                if k in ("gid", "text")
            }
        if context["operation"] != "comment":
            after = {k: task[k] for k in requested if k in task}
    diff = {
        "before": {k: before[k] for k in requested if k in before},
        "after": after,
        "before_missing": [k for k in requested if k not in before],
        "after_missing": [k for k in requested if k not in after],
    }
    if context["operation"] in ("create", "create-subtask", "comment"):
        diff["before"] = None  # New resource; no pre-existing state.
        diff["before_missing"] = []
    meta = dict(primary.get("api_meta") or {})
    meta["endpoint"] = (
        primary.get("method", "") + " " + primary.get("path", "")
    ).strip() or None
    meta["attempts"] = primary.get("attempts", [])
    return {
        "level": "info" if outcome in ("success", "help") else "error",
        "skill": "asana",
        "action": ACTIONS.get(context["operation"], context["operation"]),
        "status": outcome,
        "duration_ms": round(
            (time.perf_counter() - state.get("start", time.perf_counter())) * 1000
        ),
        "target": target,
        "requested": requested,
        "diff": diff,
        "api_meta": meta,
        "error_message": primary.get("error")
        or next((c["error"] for c in reversed(calls) if c.get("error")), None),
        "agent_context": {
            "trigger_prompt_summary": state.get("reason"),
            "dry_run": False,
        },
        "read_summary": {
            "count": (
                sum(
                    len(c["observed"])
                    for c in calls
                    if isinstance(c.get("observed"), list)
                )
                if any(isinstance(c.get("observed"), list) for c in calls)
                else (1 if task or state.get("resolved_user") else 0)
            ),
            "settings": {
                k: task[k]
                for k in (
                    "assignee",
                    "start_on",
                    "start_at",
                    "due_on",
                    "due_at",
                    "completed",
                )
                if k in task
            },
            "query": primary.get("query"),
            "has_more": bool(primary.get("next_page")),
        }
        if not writes
        else None,
    }


class AuditCompletionError(RuntimeError):
    def __init__(self, result):
        super().__init__("audit_log_finish_failed")
        self.result = result


def redact(value):
    """Exclude credential fields and redact the active token in all string values."""
    if isinstance(value, dict):
        return {
            k: (
                redact(v)[:50] + ("…" if len(redact(v)) > 50 else "")
                if k in {"notes", "text"} and isinstance(v, str)
                else redact(v)
            )
            for k, v in value.items()
            if k.lower()
            not in {
                "authorization",
                "access_token",
                "token",
                "password",
                "secret",
                "asana_access_token",
            }
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        token = os.environ.get("ASANA_ACCESS_TOKEN")
        return value.replace(token, "[REDACTED]") if token else value
    return value


def snapshot(value):
    """Keep explainable business fields, not unrelated profile/custom-field data."""
    if isinstance(value, list):
        return [snapshot(v) for v in value]
    if isinstance(value, dict):
        fields = {
            "gid",
            "name",
            "notes",
            "text",
            "assignee",
            "start_on",
            "start_at",
            "due_on",
            "due_at",
            "completed",
            "parent",
            "projects",
            "workspace",
            "memberships",
            "project",
            "section",
            "permalink_url",
            "resource_type",
            "resource_subtype",
            "created_by",
        }
        return {k: snapshot(v) for k, v in value.items() if k in fields}
    return value


def request(method, path, params=None, data=None):
    call_id = str(uuid.uuid4())
    common = {
        "run_id": RUN_ID.get() or call_id,
        "call_id": call_id,
        "method": method,
        "path": path,
    }

    def event(phase, **fields):
        append_log(dict(common, event=phase, **fields))

    state = AUDIT.get()
    prior = {}
    if state:
        prior = state["cache"].get(path, {})
        if path.endswith("/addTask") and data:
            prior = state["cache"].get("/tasks/" + data.get("task", ""), {})
    call = {
        "method": method,
        "path": path,
        "query": params,
        "requested": data,
        "before": prior,
        "attempts": [],
    }
    if state:
        state["calls"].append(call)
        state["active"] = call
    # Persist target and previous values before the mutation.
    event("api_started", query=params, requested=data, before=prior)
    try:
        result = _request(method, path, params, data)
    except BaseException as error:
        outcome = (
            "write_outcome_unknown"
            if str(error).startswith("write_outcome_unknown")
            else "failed"
        )
        details = (
            error.details
            if isinstance(error, APIError)
            else {"type": type(error).__name__}
        )
        call.update(outcome=outcome, error=details)
        try:
            event(
                "api_finished",
                outcome=outcome,
                error=details,
                api_meta=call.get("api_meta"),
                attempts=call["attempts"],
            )
        except OSError:
            raise AuditCompletionError(
                {"outcome": outcome, "next_action": "Inspect target before retrying"}
            ) from None
        raise
    call.update(
        outcome="success",
        observed=snapshot(result.get("data")),
        next_page=result.get("next_page"),
    )
    if state and isinstance(result.get("data"), dict) and method == "GET":
        state["cache"][path] = snapshot(result["data"])
    try:
        event(
            "api_finished",
            api_meta=call.get("api_meta"),
            attempts=call["attempts"],
            outcome="success",
            observed=snapshot(result.get("data")),
            next_page=result.get("next_page"),
        )
    except OSError:
        raise AuditCompletionError(result) from None
    return result


class APIError(RuntimeError):
    def __init__(self, category, status, messages):
        super().__init__(category)
        self.details = {"error": category, "http_status": status, "messages": messages}


def verify_task(gid, written):
    try:
        return request("GET", "/tasks/" + gid)
    except AuditCompletionError:
        raise
    except (RuntimeError, ValueError, KeyError, OSError):
        return {
            "status": "written_but_unverified",
            "gid": gid,
            "data": written,
            "error": "verification_failed",
            "next_action": "GET this GID to verify; do not resend the write",
        }


def _request(method, path, params=None, data=None):
    token = os.environ.get("ASANA_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("ASANA_ACCESS_TOKEN is required")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps({"data": data}).encode() if data is not None else None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            body,
            {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                state = AUDIT.get()
                if state and state.get("active"):
                    meta = api_metadata(response)
                    state["active"]["api_meta"] = meta
                    state["active"]["attempts"].append(meta)
                return json.load(response)
        except urllib.error.HTTPError as exc:
            state = AUDIT.get()
            meta = api_metadata(exc)
            if state and state.get("active"):
                state["active"]["api_meta"] = meta
                state["active"]["attempts"].append(meta)
            if exc.code == 429 and attempt < 2:
                delay = float(exc.headers.get("Retry-After", "1"))
                if 0 <= delay <= 60:
                    append_log(
                        {
                            "event": "api_retry",
                            "run_id": RUN_ID.get(),
                            "endpoint": method + " " + path,
                            "api_meta": meta,
                            "retry_number": attempt + 1,
                        }
                    )
                    time.sleep(delay)
                    continue
            labels = {
                401: "authentication_failed",
                403: "permission_denied",
                402: "paid_search_unavailable",
                404: "not_found",
                429: "rate_limited",
            }
            messages = []
            try:
                payload = json.loads(exc.read())
                for item in payload.get("errors", []):
                    message = item.get("message")
                    if isinstance(message, str):
                        messages.append(message.replace(token, "[REDACTED]")[:1000])
            except (ValueError, AttributeError, TypeError, OSError):
                pass
            raise APIError(
                labels.get(exc.code, "http_error"), exc.code, messages
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError(
                "network_failure"
                if method == "GET"
                else "write_outcome_unknown: inspect target before retrying"
            ) from None


def listing(path, params, maximum):
    items = []
    while True:
        page = request("GET", path, dict(params, limit=min(100, maximum - len(items))))
        items.extend(page["data"])
        offset = (page.get("next_page") or {}).get("offset")
        if not offset or len(items) >= maximum:
            return {"data": items, "has_more": bool(offset), "next_offset": offset}
        params = dict(params, offset=offset)


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=[
            "me",
            "workspaces",
            "users",
            "resolve-user",
            "projects",
            "sections",
            "tasks",
            "subtasks",
            "create-subtask",
            "task",
            "comments",
            "search",
            "create",
            "update",
            "comment",
            "move",
        ],
    )
    parser.add_argument("--reason", help="操作理由を1行で指定（ログに記録）")
    parser.add_argument("--email", help="Asana登録メールアドレス")
    parser.add_argument("--gid")
    parser.add_argument("--workspace")
    parser.add_argument("--project")
    parser.add_argument("--section")
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--data", help="JSON object; null explicitly clears a field")
    parser.add_argument("--data-file")
    parser.add_argument("--max-items", type=int, default=100)
    parser.add_argument("--offset")
    parser.add_argument(
        "--query",
        default="{}",
        help="JSON API filters; search is explicitly paid workspace search",
    )
    args = parser.parse_args(argv)
    if AUDIT.get() is not None:
        AUDIT.get()["reason"] = (
            " ".join(args.reason.split())[:300] if args.reason else None
        )

    def need(value, label):
        if not value:
            parser.error(label + " is required")
        return value

    if args.max_items < 1:
        parser.error("--max-items must be positive")
    try:
        params = json.loads(args.query)
    except ValueError:
        parser.error("--query must be a JSON object")
    if not isinstance(params, dict):
        parser.error("--query must be a JSON object")
    if args.offset:
        params["offset"] = args.offset
    op = args.operation
    if op == "me":
        return request("GET", "/users/me")
    if op == "resolve-user":
        workspace = need(args.workspace, "--workspace")
        email = need(args.email, "--email").strip()
        if (
            email.count("@") != 1
            or not all(email.split("@"))
            or any(char.isspace() or char in "/?#" for char in email)
        ):
            parser.error("--email must be an email address")
        if AUDIT.get() is not None:
            AUDIT.get()["lookup_email"] = email
        user = request(
            "GET",
            "/users/" + urllib.parse.quote(email, safe=""),
            {"opt_fields": "name,email,workspaces.gid"},
        )["data"]
        if not isinstance(user.get("workspaces"), list):
            raise RuntimeError("workspace_membership_unverified")
        if not any(w.get("gid") == workspace for w in user["workspaces"]):
            raise RuntimeError("user_not_in_workspace")
        resolved = {
            "gid": user["gid"],
            "name": user.get("name"),
            "email": user.get("email"),
            "workspace_gid": workspace,
            "membership_verified": True,
        }
        if AUDIT.get() is not None:
            AUDIT.get()["resolved_user"] = resolved
        return {"data": resolved}
    paths = {
        "workspaces": "/workspaces",
        "users": "/workspaces/{}/users",
        "projects": "/workspaces/{}/projects",
        "sections": "/projects/{}/sections",
        "tasks": "/projects/{}/tasks",
        "comments": "/tasks/{}/stories",
        "subtasks": "/tasks/{}/subtasks",
    }
    if op in paths:
        path = paths[op]
        if "{}" in path:
            value = (
                args.workspace
                if op in ("users", "projects")
                else (args.project if op in ("sections", "tasks") else args.gid)
            )
            path = path.format(need(value, "parent GID"))
        return listing(path, params, args.max_items)
    if op == "search":
        return request(
            "GET",
            "/workspaces/" + need(args.workspace, "--workspace") + "/tasks/search",
            dict(params, limit=min(args.max_items, 100)),
        )
    if op == "task":
        return request("GET", "/tasks/" + need(args.gid, "--gid"), params)
    if op in ("create", "update", "create-subtask"):
        if args.data and args.data_file:
            parser.error("choose --data or --data-file")
        if args.data_file:
            with open(args.data_file, encoding="utf-8") as stream:
                data = json.load(stream)
        else:
            data = json.loads(need(args.data, "--data or --data-file"))
        allowed = {
            "name",
            "notes",
            "assignee",
            "due_on",
            "due_at",
            "start_on",
            "start_at",
            "completed",
        }
        if op == "create":
            allowed |= {"projects", "workspace"}
        if not isinstance(data, dict) or not data or set(data) - allowed:
            parser.error("unsupported or empty task fields")
        path = "/tasks"
        if op == "update":
            path += "/" + need(args.gid, "--gid")
            request("GET", path)
        if op == "create-subtask":
            parent = need(args.gid, "--gid (parent task)")
            if not data.get("name"):
                parser.error("subtask name is required")
            request("GET", "/tasks/" + parent)
            path = "/tasks/" + parent + "/subtasks"
        result = request("PUT" if op == "update" else "POST", path, data=data)
        return verify_task(result["data"]["gid"], result["data"])
    gid = need(args.gid, "--gid")
    task = request(
        "GET",
        "/tasks/" + gid,
        {
            "opt_fields": "name,permalink_url,memberships.project.gid,memberships.section.gid,memberships.section.name"
        },
    )
    if op == "comment":
        if args.text and args.text_file:
            parser.error("choose --text or --text-file")
        if args.text_file:
            with open(args.text_file, encoding="utf-8") as stream:
                content = stream.read()
        else:
            content = need(args.text, "--text or --text-file")
        return request("POST", "/tasks/" + gid + "/stories", data={"text": content})
    section = need(args.section, "--section")
    target = request("GET", "/sections/" + section, {"opt_fields": "project.gid"})
    projects = [m["project"]["gid"] for m in task["data"]["memberships"]]
    if target["data"]["project"]["gid"] not in projects:
        parser.error("section must belong to an existing task project")
    request("POST", "/sections/" + section + "/addTask", data={"task": gid})
    return verify_task(gid, {"gid": gid})


def append_log(record):
    path = Path.home() / ".agents/logs/asana.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # A separate, fixed-position lock works with Windows byte-range locking too.
    with (path.parent / "asana.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            entry = dict(record, schema_version=3)
            entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(redact(entry), ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    token = RUN_ID.set(str(uuid.uuid4()))
    audit_token = AUDIT.set(
        {"start": time.perf_counter(), "calls": [], "cache": {}, "reason": None}
    )
    try:
        return run_main(argv)
    finally:
        RUN_ID.reset(token)
        AUDIT.reset(audit_token)


def run_main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    operations = {
        "me",
        "workspaces",
        "users",
        "resolve-user",
        "projects",
        "sections",
        "tasks",
        "subtasks",
        "create-subtask",
        "task",
        "comments",
        "search",
        "create",
        "update",
        "comment",
        "move",
    }
    context = {
        "run_id": RUN_ID.get(),
        "operation": args[0] if args and args[0] in operations else "invalid",
        "cwd": str(Path.cwd()),
    }
    # Only numeric identifiers are logged; never serialize raw arguments or API bodies.
    for flag in ("--gid", "--workspace", "--project", "--section"):
        for index, value in enumerate(args):
            candidate = (
                args[index + 1]
                if value == flag and index + 1 < len(args)
                else (value.split("=", 1)[1] if value.startswith(flag + "=") else "")
            )
            if candidate.isascii() and candidate.isdigit():
                context[flag[2:]] = candidate

    def record(event, **fields):
        if event == "finished":
            fields.update(operation_summary(context, fields.get("outcome", "failed")))
        append_log(
            dict(
                context,
                timestamp=datetime.now(timezone.utc).isoformat(),
                event=event,
                **fields,
            )
        )

    try:
        record("started")
    except OSError:
        raise RuntimeError("audit_log_unavailable: operation was not started") from None
    try:
        result = _main(args)
    except AuditCompletionError as error:
        return {
            "status": "audit_log_finish_failed",
            "result": error.result,
            "run_id": context["run_id"],
            "next_action": "Inspect target and log storage; do not resend writes",
        }
    except BaseException as error:
        outcome = "failed"
        if isinstance(error, RuntimeError) and str(error).startswith(
            "write_outcome_unknown"
        ):
            outcome = "write_outcome_unknown"
        if isinstance(error, SystemExit) and error.code == 0:
            outcome = "help"
        try:
            record(
                "finished",
                outcome=outcome,
                error_type=type(error).__name__,
                resolution_error=str(error)
                if str(error)
                in {"user_not_in_workspace", "workspace_membership_unverified"}
                else None,
            )
        except OSError:
            raise RuntimeError(
                "audit_log_finish_failed: inspect target; do not resend writes"
            ) from None
        raise
    data = result.get("data")
    gid = result.get("gid") or (data.get("gid") if isinstance(data, dict) else None)
    fields = {"outcome": result.get("status", "success")}
    if isinstance(gid, str) and gid.isascii() and gid.isdigit():
        fields["result_gid"] = gid
    try:
        record("finished", **fields)
    except OSError:
        # Return the known result so callers do not mistake a logging failure for a failed write.
        return {
            "status": "audit_log_finish_failed",
            "result": result,
            "next_action": "Inspect result and log storage; do not resend writes",
            "run_id": context["run_id"],
        }
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except APIError as error:
        print(json.dumps(error.details, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
