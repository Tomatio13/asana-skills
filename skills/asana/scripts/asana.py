#!/usr/bin/env python3
"""Small dependency-free Asana client."""

import argparse
from contextvars import ContextVar
import uuid
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree
from xml.sax.saxutils import escape

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
    "dependencies": "list_dependencies",
    "add-dependency": "add_dependency",
    "add-project": "add_project",
    "custom-fields": "list_custom_fields",
    "set-custom-field": "set_custom_field",
    "attachments": "list_attachments",
    "upload-attachment": "upload_attachment",
    "delete-attachment": "delete_attachment",
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
    for argument, field in (
        ("depends-on", "dependency_gid"),
        ("field", "field_gid"),
        ("attachment", "attachment_gid"),
    ):
        if context.get(argument):
            target[field] = context[argument]
    if context["operation"] == "add-project":
        target["destination_project_gid"] = context.get("project")
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
    if "comment_mentions" in state:
        requested = {
            "text": state["comment_text"],
            "mentions": state["comment_mentions"],
        }
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
            if "confirmed_mentions" in state:
                after["mentions"] = state["confirmed_mentions"]
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
    change = state.get("change")
    if change:
        target.update(change["target"])
        requested = change["requested"]
        diff = change["diff"]
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
                preview(v)
                if k in {"notes", "text", "text_value", "html_text"}
                and isinstance(v, str)
                else {field: preview(item) for field, item in v.items()}
                if k == "custom_fields" and isinstance(v, dict)
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


def preview(value):
    safe = redact(value)
    if isinstance(safe, str) and len(safe) > 50:
        return safe[:50] + "…"
    return safe


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


def request(method, path, params=None, data=None, *, upload=None):
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
        if method != "GET" and state.get("change"):
            prior = state["change"]["diff"]["before"]
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
        if upload is None:
            result = _request(method, path, params, data)
        else:
            result = _request(method, path, params, data, upload=upload)
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


def _request(method, path, params=None, data=None, *, upload=None):
    token = os.environ.get("ASANA_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("ASANA_ACCESS_TOKEN is required")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    content_type = "application/json"
    if upload is None:
        body = json.dumps({"data": data}).encode() if data is not None else None
    else:
        body, content_type = attachment_body(data["parent"], data["name"], upload)
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            body,
            {
                "Authorization": "Bearer " + token,
                "Content-Type": content_type,
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
                try:
                    return json.load(response)
                except ValueError:
                    if method == "GET":
                        raise
                    raise RuntimeError(
                        "write_outcome_unknown: inspect target before retrying"
                    ) from None
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
                402: "paid_search_unavailable"
                if path.endswith("/tasks/search")
                else "plan_required",
                404: "not_found",
                429: "rate_limited",
            }
            messages = []
            try:
                payload = json.loads(exc.read())
                for item in payload.get("errors", []):
                    message = item.get("message")
                    if isinstance(message, str):
                        for value in (data or {}).get("custom_fields", {}).values():
                            if isinstance(value, str) and len(value) > 50:
                                message = message.replace(value, preview(value))
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
        limit = 100 if maximum is None else min(100, maximum - len(items))
        page = request("GET", path, dict(params, limit=limit))
        items.extend(page["data"])
        offset = (page.get("next_page") or {}).get("offset")
        if not offset or (maximum is not None and len(items) >= maximum):
            return {"data": items, "has_more": bool(offset), "next_offset": offset}
        params = dict(params, offset=offset)


DEPENDENCY_FIELDS = "gid,name,completed,permalink_url"
ATTACHMENT_FIELDS = "gid,name,host,view_url,permanent_url"
ATTACHMENT_LIMIT = 100_000_000
TASK_IDENTITY = "gid,name,permalink_url"
TASK_MEMBERSHIPS = (
    TASK_IDENTITY + ",workspace.gid,memberships.project.gid,memberships.section.gid"
)
FIELD_SETTINGS = (
    "custom_field.gid,custom_field.name,custom_field.resource_subtype,"
    "custom_field.is_formula_field,custom_field.id_prefix,"
    "custom_field.enum_options.gid,custom_field.enum_options.name,"
    "custom_field.enum_options.enabled"
)


def require_gid(value, flag, parser):
    if not value or not value.isascii() or not value.isdigit():
        parser.error(flag + " must be an Asana GID")
    return value


def pagination(params, fields):
    result = {"opt_fields": fields}
    if "offset" in params:
        result["offset"] = params["offset"]
    return result


def dependencies_path(gid):
    return f"/tasks/{gid}/dependencies"


def field_settings_path(project):
    return f"/projects/{project}/custom_field_settings"


def audit_change(target, key, previous, expected):
    change = {
        "target": target,
        "requested": {key: expected},
        "diff": {
            "before": {key: previous},
            "after": {},
            "before_missing": [],
            "after_missing": [key],
        },
    }
    state = AUDIT.get()
    if state is not None:
        state["change"] = change
    return change


def verify_change(gid, change, read, matches):
    key, expected = next(iter(change["requested"].items()))
    try:
        result, actual = read()
        change["diff"].update(after={key: actual}, after_missing=[])
        if matches(actual):
            return result
        error = "verification_mismatch"
    except AuditCompletionError:
        raise
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        error = "verification_failed"
    return {
        "status": "written_but_unverified",
        "gid": gid,
        "expected": change["requested"],
        "error": error,
        "next_action": "Read the target state to verify; do not resend the write",
    }


def attachment_body(parent, name, contents):
    boundary = uuid.uuid4().hex
    # The live API preserves UTF-8 names and displays percent-encoded names literally.
    filename = (
        name.replace("\\", "\\\\")
        .replace('"', "%22")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    headers = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="parent"\r\n\r\n{parent}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    )
    return (
        headers.encode("utf-8") + contents + f"\r\n--{boundary}--\r\n".encode("ascii"),
        f"multipart/form-data; boundary={boundary}",
    )


def attachment_list(gid):
    return listing(
        "/attachments", {"parent": gid, "opt_fields": ATTACHMENT_FIELDS}, None
    )


def find_attachment(items, gid):
    return next((item for item in items if item["gid"] == gid), None)


def upload_attachment(gid, path):
    if not path.is_file():
        raise ValueError("attachment_file_must_be_a_regular_file")
    try:
        with path.open("rb") as stream:
            contents = stream.read(ATTACHMENT_LIMIT + 1)
    except OSError:
        raise ValueError("attachment_file_unreadable") from None
    if len(contents) > ATTACHMENT_LIMIT:
        raise ValueError("attachment_file_exceeds_100_mb")
    request("GET", f"/tasks/{gid}", {"opt_fields": TASK_IDENTITY})
    change = audit_change({}, "attachment", None, {"name": path.name})
    written = request(
        "POST",
        "/attachments",
        data={"parent": gid, "name": path.name},
        upload=contents,
    )
    attachment_gid = (written.get("data") or {}).get("gid")
    if attachment_gid:
        change["target"]["attachment_gid"] = attachment_gid

    def read():
        result = attachment_list(gid)
        item = find_attachment(result["data"], attachment_gid)
        return {"data": item}, snapshot(item)

    return verify_change(gid, change, read, lambda actual: actual is not None)


def delete_attachment(gid, attachment_gid):
    request("GET", f"/tasks/{gid}", {"opt_fields": TASK_IDENTITY})
    item = find_attachment(attachment_list(gid)["data"], attachment_gid)
    if item is None:
        raise ValueError("attachment_not_found_on_task")
    change = audit_change(
        {"attachment_gid": attachment_gid}, "attachment", snapshot(item), None
    )
    request("DELETE", f"/attachments/{attachment_gid}")

    def read():
        result = attachment_list(gid)
        item = find_attachment(result["data"], attachment_gid)
        return {
            "data": {"gid": attachment_gid, "task_gid": gid, "deleted": True}
        }, snapshot(item)

    return verify_change(gid, change, read, lambda actual: actual is None)


def comment_with_mentions(gid, content, mentions):
    for user_gid in mentions:
        request("GET", f"/users/{user_gid}", {"opt_fields": "gid"})
    links = " ".join(f'<a data-asana-gid="{user_gid}"/>' for user_gid in mentions)
    html = f"<body>{links} {escape(content)}</body>"
    state = AUDIT.get()
    if state is not None:
        state.update(comment_text=content, comment_mentions=mentions)
    fields = {"opt_fields": "gid,text,html_text"}
    result = request("POST", f"/tasks/{gid}/stories", fields, {"html_text": html})
    story = result.get("data") or {}
    story_gid = story.get("gid")
    try:
        if story_gid and not story.get("html_text"):
            result = request("GET", f"/stories/{story_gid}", fields)
            story = result["data"]
        body = ElementTree.fromstring(story["html_text"])
        actual = list(
            dict.fromkeys(
                link.attrib["data-asana-gid"]
                for link in body.iter("a")
                if link.get("data-asana-type") == "user" and link.get("data-asana-gid")
            )
        )
        if state is not None:
            state["confirmed_mentions"] = actual
        if story_gid and set(mentions).issubset(actual):
            return dict(result, mentions=actual)
    except AuditCompletionError:
        raise
    except (
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        ElementTree.ParseError,
        OSError,
    ):
        pass
    return {
        "status": "written_but_unverified",
        "gid": story_gid,
        "task_gid": gid,
        "expected_mentions": mentions,
        "next_action": "Read the existing story to verify; do not repost",
    }


def add_dependency(gid, prerequisite):
    request("GET", f"/tasks/{prerequisite}", {"opt_fields": TASK_IDENTITY})
    task = request("GET", f"/tasks/{gid}", {"opt_fields": TASK_IDENTITY})
    path = dependencies_path(gid)

    def read():
        result = listing(path, {"opt_fields": DEPENDENCY_FIELDS}, None)
        return result, [item["gid"] for item in result["data"]]

    _, previous = read()
    if prerequisite in previous:
        return dict(task, already_set=True, dependency_gid=prerequisite)
    change = audit_change(
        {"dependency_gid": prerequisite}, "dependencies", previous, [prerequisite]
    )
    request(
        "POST", f"/tasks/{gid}/addDependencies", data={"dependencies": [prerequisite]}
    )
    return verify_change(gid, change, read, lambda actual: prerequisite in actual)


def task_memberships(task):
    return {
        item["project"]["gid"]: (item.get("section") or {}).get("gid")
        for item in task["memberships"]
    }


def add_project(gid, project, section):
    path = f"/tasks/{gid}"
    params = {"opt_fields": TASK_MEMBERSHIPS}
    task = request("GET", path, params)
    destination = request(
        "GET", f"/projects/{project}", {"opt_fields": "workspace.gid"}
    )["data"]
    workspace = (task["data"].get("workspace") or {}).get("gid")
    if not workspace or workspace != (destination.get("workspace") or {}).get("gid"):
        raise RuntimeError("task_and_project_workspace_mismatch")
    if section:
        parent = request("GET", f"/sections/{section}", {"opt_fields": "project.gid"})[
            "data"
        ]
        if (parent.get("project") or {}).get("gid") != project:
            raise RuntimeError("section_not_in_destination_project")
    memberships = task_memberships(task["data"])
    if project in memberships:
        current = memberships[project]
        if section and section != current:
            return {
                "status": "already_in_project_different_section",
                "gid": gid,
                "project_gid": project,
                "current_section_gid": current,
                "requested_section_gid": section,
            }
        return dict(task, already_set=True)
    placement = {"project": project}
    if section:
        placement["section"] = section
    change = audit_change(
        {"destination_project_gid": project}, "project_membership", None, placement
    )

    def read():
        result = request("GET", path, params)
        after = task_memberships(result["data"])
        actual = (
            {"project": project, "section": after[project]}
            if project in after
            else None
        )
        if memberships.keys() - after.keys():
            raise RuntimeError("original_project_membership_missing")
        return result, actual

    request("POST", path + "/addProject", data=placement)
    return verify_change(
        gid,
        change,
        read,
        lambda actual: (
            actual is not None and (section is None or actual["section"] == section)
        ),
    )


def set_custom_field(gid, project, field_gid, value):
    path = f"/tasks/{gid}"
    params = {
        "opt_fields": TASK_MEMBERSHIPS
        + ",custom_fields.gid,custom_fields.text_value,custom_fields.number_value,custom_fields.enum_value.gid"
    }
    task = request("GET", path, params)
    if project not in task_memberships(task["data"]):
        raise RuntimeError("task_not_in_field_project")
    settings = listing(
        field_settings_path(project), {"opt_fields": FIELD_SETTINGS}, None
    )
    field = next(
        (
            item["custom_field"]
            for item in settings["data"]
            if item["custom_field"]["gid"] == field_gid
        ),
        None,
    )
    if field is None:
        raise RuntimeError("custom_field_not_in_project")
    subtype = field["resource_subtype"]
    if (
        field.get("is_formula_field")
        or field.get("id_prefix") is not None
        or subtype not in ("text", "number", "enum")
    ):
        raise RuntimeError("custom_field_type_not_writable")
    if value is not None:
        if subtype == "text":
            valid = isinstance(value, str) and len(value) <= 1024
        elif subtype == "number":
            valid = type(value) is int or (
                type(value) is float and math.isfinite(value)
            )
        else:
            valid = any(
                option["gid"] == value and option.get("enabled")
                for option in field["enum_options"]
            )
        if not valid:
            raise ValueError("invalid_custom_field_value")

    def field_value(task_data):
        for item in task_data["custom_fields"]:
            if item["gid"] == field_gid:
                current = item[subtype + "_value"]
                return (current or {}).get("gid") if subtype == "enum" else current
        raise RuntimeError("custom_field_not_on_task")

    previous = field_value(task["data"])
    change = audit_change(
        {
            "field_gid": field_gid,
            "field_name": field["name"],
            "field_project_gid": project,
        },
        subtype + "_value",
        previous,
        value,
    )

    def read():
        result = request("GET", path, params)
        return result, field_value(result["data"])

    request("PUT", path, data={"custom_fields": {field_gid: value}})
    return verify_change(gid, change, read, lambda actual: actual == value)


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=ACTIONS,
    )
    parser.add_argument("--reason", help="操作理由を1行で指定（ログに記録）")
    parser.add_argument("--email", help="Asana登録メールアドレス")
    parser.add_argument("--gid")
    parser.add_argument("--workspace")
    parser.add_argument("--project")
    parser.add_argument("--section")
    parser.add_argument("--depends-on")
    parser.add_argument("--field")
    parser.add_argument("--file", help="Local attachment file to upload")
    parser.add_argument("--attachment", help="Attachment GID to delete")
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--mention", action="append", default=[], metavar="USER_GID")
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

    def load_data():
        if args.data is not None and args.data_file:
            parser.error("choose --data or --data-file")
        if args.data_file:
            with open(args.data_file, encoding="utf-8") as stream:
                data = json.load(stream)
        else:
            data = json.loads(need(args.data, "--data or --data-file"))
        return data

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
    if args.mention and op != "comment":
        parser.error("--mention is only supported by comment")
    if op in ("dependencies", "attachments", "custom-fields"):
        if op == "custom-fields":
            project = require_gid(args.project, "--project", parser)
            return listing(
                field_settings_path(project),
                pagination(params, FIELD_SETTINGS),
                args.max_items,
            )
        gid = require_gid(args.gid, "--gid", parser)
        if op == "dependencies":
            return listing(
                dependencies_path(gid),
                pagination(params, DEPENDENCY_FIELDS),
                args.max_items,
            )
        request("GET", f"/tasks/{gid}", {"opt_fields": TASK_IDENTITY})
        return listing(
            "/attachments",
            dict(pagination(params, ATTACHMENT_FIELDS), parent=gid),
            args.max_items,
        )
    if op in ("upload-attachment", "delete-attachment"):
        gid = require_gid(args.gid, "--gid", parser)
        if op == "upload-attachment":
            return upload_attachment(gid, Path(need(args.file, "--file")))
        return delete_attachment(
            gid, require_gid(args.attachment, "--attachment", parser)
        )
    if op in ("add-dependency", "add-project", "set-custom-field"):
        gid = require_gid(args.gid, "--gid", parser)
        if op == "add-dependency":
            prerequisite = require_gid(args.depends_on, "--depends-on", parser)
            if gid == prerequisite:
                parser.error("a task cannot depend on itself")
            return add_dependency(gid, prerequisite)
        project = require_gid(args.project, "--project", parser)
        if op == "add-project":
            section = (
                require_gid(args.section, "--section", parser)
                if args.section is not None
                else None
            )
            return add_project(gid, project, section)
        field = require_gid(args.field, "--field", parser)
        data = load_data()
        if not isinstance(data, dict) or set(data) != {"value"}:
            parser.error("expected a JSON object containing only value")
        return set_custom_field(gid, project, field, data["value"])
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
        data = load_data()
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
        if args.mention:
            mentions = list(
                dict.fromkeys(
                    require_gid(value, "--mention", parser) for value in args.mention
                )
            )
            return comment_with_mentions(gid, content, mentions)
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
    context = {
        "run_id": RUN_ID.get(),
        "operation": args[0] if args and args[0] in ACTIONS else "invalid",
        "cwd": str(Path.cwd()),
    }
    # Only numeric identifiers are logged; never serialize raw arguments or API bodies.
    for flag in (
        "--gid",
        "--workspace",
        "--project",
        "--section",
        "--depends-on",
        "--field",
        "--attachment",
    ):
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
