"""Bounded native NDJSON events and opt-in Claude bidirectional control.

This module owns no processes, threads, permissions, retries or durable state.
The runner owns the pipes and persists a request before calling this adapter.
``send`` must write the entire supplied byte string (or raise); ``emit`` receives
allowlisted metadata only. Feed stdout chunks, then call ``finish`` at EOF.

Wire shapes follow anthropics/claude-agent-sdk-python at
6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d. A successful initialize response
proves that exchange worked, not that later messages or interrupts were applied.
User echoes and control replies are receipts, never proof of completed effects.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
import uuid


MAX_FRAME_BYTES = 2_097_152
MAX_REQUESTS = 4096
_POINTER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_CREDENTIAL = re.compile(r"(?i)(?:^(?:sk|pk|ghp|gho|github_pat|xox[baprs])[-_]|^eyJ|(?:token|password|secret|authorization)[=:])")
_NAMESPACE = uuid.UUID("668d4f97-48b3-499a-830b-301e24f77283")
_TERMINAL_TASKS = {"completed", "failed", "stopped", "killed"}
_TASK_STATUSES = _TERMINAL_TASKS | {"pending", "running", "in_progress"}
_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell",
          "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Task", "Skill",
          "TodoWrite", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
          "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "ToolSearch", "MCP"}
_USAGE_KEYS = {"input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "cached_input_tokens", "total_tokens"}
_RESULT_REASONS = {"completed", "max_turns", "aborted_streaming", "aborted_tools",
                   "max_budget", "error", "stop_sequence", "end_turn", "tool_use"}


class NativeProtocolError(RuntimeError):
    """A fixed, content-free protocol or lifecycle diagnostic."""


def duplex_arguments() -> list[str]:
    """Additional flags for Claude/Astra's existing print stream-json command.

    Probe advertised flags before launch, then require initialize acknowledgment
    before sending a prompt. Do not fall back after input might have executed.
    Native rules still settle permissions first; unresolved decisions reach our
    fail-closed stdio responder. No settings or system-prompt flags are injected.
    """
    return ["--input-format", "stream-json", "--replay-user-messages",
            "--permission-prompt-tool", "stdio"]


def _pointer(value: object) -> str | None:
    if isinstance(value, str) and _POINTER.fullmatch(value) and not _CREDENTIAL.search(value):
        return value
    return None


def _tool(value: object) -> str | None:
    if isinstance(value, str):
        if value in _TOOLS:
            return value
        if value.startswith("mcp__"):
            return "MCP"
    return None


def _usage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: amount for key, amount in value.items()
            if key in _USAGE_KEYS and type(amount) is int and 0 <= amount <= 2**53}


def _pointers(frame: dict, *keys: str) -> dict:
    return {key: pointer for key in keys if (pointer := _pointer(frame.get(key))) is not None}


def _event(kind: str, frame: dict, **fields) -> dict:
    return {"kind": kind, **_pointers(frame, "session_id", "parent_tool_use_id"), **fields}


def normalize_event(frame: object, harness: str = "claude") -> list[dict]:
    """Select public event metadata; never pass through arbitrary native fields.

    No prompts, output text, tool arguments, thinking, provider configuration,
    file paths, descriptions or native error strings are returned. Unknown
    frames and content blocks are ignored. IDs are explicitly allowed pointers,
    not file paths. This is suitable for timelines, not transcript recreation.
    """
    if not isinstance(frame, dict):
        return []
    kind = frame.get("type")
    if harness == "codex":
        if kind == "thread.started":
            pointer = _pointer(frame.get("thread_id"))
            return [{"kind": "native_session", "session_id": pointer}] if pointer else []
        if kind in ("turn.started", "turn.completed", "turn.failed"):
            status = {"turn.started": "started", "turn.completed": "succeeded", "turn.failed": "failed"}[kind]
            event = {"kind": "native_turn", "status": status}
            if usage := _usage(frame.get("usage")):
                event["usage"] = usage
            return [event]
        if kind == "error":
            return [{"kind": "native_turn", "status": "failed"}]
        if kind not in ("item.started", "item.completed"):
            return []
        item = frame.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        if item_type == "agent_message":
            return [{"kind": "native_message", "role": "assistant"}]
        known = {"command_execution": "Bash", "mcp_tool_call": "MCP",
                 "file_change": "FileChange", "web_search": "WebSearch"}
        if not isinstance(item_type, str) or item_type not in known:
            return []
        event = {"kind": "native_tool", "tool": known[item_type],
                 "status": "started" if kind == "item.started" else "completed"}
        if pointer := _pointer(item.get("id")):
            event["item_id"] = pointer
        if item.get("status") == "failed":
            event["status"] = "failed"
        return [event]

    if kind == "system":
        subtype = frame.get("subtype")
        if subtype == "init":
            return [_event("native_session", frame)]
        if subtype == "session_state_changed" and frame.get("state") in ("idle", "running", "requires_action"):
            return [_event("native_session_state", frame, status=frame["state"])]
        if subtype in ("task_started", "task_progress", "task_notification", "task_updated"):
            event = _event("native_task", frame, **_pointers(frame, "task_id", "tool_use_id"))
            status = frame.get("status")
            if subtype == "task_updated" and isinstance(frame.get("patch"), dict):
                status = frame["patch"].get("status")
            if subtype == "task_started":
                status = "started"
            elif subtype == "task_progress":
                status = "running"
            if isinstance(status, str) and status in _TASK_STATUSES | {"started"}:
                event["status"] = status
            if frame.get("task_type") in ("local_agent", "local_workflow", "local_bash", "remote_agent"):
                event["task_type"] = frame["task_type"]
            if usage := _usage(frame.get("usage")):
                event["usage"] = usage
            return [event]
        return []
    if kind == "result":
        success = frame.get("subtype") == "success" and frame.get("is_error") is False
        event = _event("native_turn", frame, status="succeeded" if success else "failed")
        if frame.get("terminal_reason") in ("aborted_streaming", "aborted_tools"):
            event["status"] = "interrupted"
        reason = frame.get("terminal_reason")
        if isinstance(reason, str) and reason in _RESULT_REASONS:
            event["terminal_reason"] = reason
        if usage := _usage(frame.get("usage")):
            event["usage"] = usage
        for key in ("duration_ms", "duration_api_ms", "num_turns"):
            value = frame.get(key)
            if type(value) is int and 0 <= value <= 2**53:
                event[key] = value
        cost = frame.get("total_cost_usd")
        if type(cost) in (int, float) and 0 <= cost <= 10**9 and math.isfinite(cost):
            event["total_cost_usd"] = cost
        return [event]
    if kind == "stream_event":
        inner = frame.get("event")
        if isinstance(inner, dict) and inner.get("type") == "message_start":
            return [_event("native_turn", frame, status="started")]
        return []
    if kind not in ("assistant", "user"):
        return []
    message = frame.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [_event("native_message", frame, role=kind)]
    if not isinstance(content, list):
        return []
    events = []
    has_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            has_text = True
        elif block.get("type") == "tool_use":
            event = _event("native_tool", frame, status="started")
            if tool := _tool(block.get("name")):
                event["tool"] = tool
            if pointer := _pointer(block.get("id")):
                event["tool_use_id"] = pointer
            events.append(event)
        elif block.get("type") == "tool_result":
            event = _event("native_tool", frame,
                           status="failed" if block.get("is_error") is True else "completed")
            if pointer := _pointer(block.get("tool_use_id")):
                event["tool_use_id"] = pointer
            events.append(event)
    if has_text:
        events.append(_event("native_message", frame, role=kind))
    return events


class NativeSession:
    """Single-controller protocol state machine; callbacks must not reenter it.

    ``requests`` records sent/acknowledged/rejected/unacknowledged, never applied.
    A partial write or lost acknowledgment may have executed effects: neither
    this class nor a replacement controller may automatically resend it.

    Turn/task counters inform runner shutdown, but cannot prove a run boundary:
    a task completion can enqueue a parent continuation after its ledger entry
    clears. They track only local agents/workflows, not indefinite shell jobs.
    """

    def __init__(self, send: Callable[[bytes], None], emit: Callable[[dict], None] | None = None,
                 *, max_frame_bytes: int = MAX_FRAME_BYTES):
        if type(max_frame_bytes) is not int or not 128 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 128 and 2097152")
        self.send = send
        self.emit = emit or (lambda event: None)
        self.max_frame_bytes = max_frame_bytes
        self.initialized = False
        self.closed = False
        self.protocol_error: str | None = None
        self.native_session_id: str | None = None
        self.sent_turns = 0
        self.completed_turns = 0
        self.active_tasks: set[str] = set()
        self.last_result_success: bool | None = None
        self.terminal_error = False
        self.requests: dict[str, dict] = {}
        self._wire_requests: dict[str, str] = {}
        self._buffer = bytearray()
        self._initialize_sent = False
        self._results: set[str] = set()
        self._pending_inputs: set[str] = set()
        self._overlapping_inputs = False
        self._native_state: str | None = None
        self._queued_turn_count: int | None = None
        self._completion_metadata_invalid = False
        self._completion_evidence: str | None = None

    @property
    def input_drained(self) -> bool:
        """Whether observed input completion permits the runner's drain heuristic.

        This is not proof that effects or background continuations have ended.
        The runner must also check its task ledger and durable input queue.
        SDK 0.3.282's result.user_message_uuids identifies all prompts consumed
        by a turn, including merged batches; one result is NOT one input. The
        queued_turn_count snapshot counts pending sends, not future results.
        Observed native idle is a turn boundary, not evidence that an unmatched
        prompt was consumed (a queue may contain more turns).

        Older producers get a narrow single-input fallback: an acknowledged
        input and attributable result, with no history of overlapping inputs.
        Ambiguous old multi-input streams remain unverified until explicit
        receipts arrive or the controller's existing timeout/stop boundary.
        """
        return bool(self.initialized and self.sent_turns and self._completion_evidence
                    and not self.protocol_error and not self._pending_inputs
                    and not self._completion_metadata_invalid
                    and not self._queued_turn_count
                    and self._native_state in (None, "idle"))

    @property
    def input_drain_evidence(self) -> str:
        if self.input_drained:
            return self._completion_evidence
        if self._pending_inputs:
            if self._overlapping_inputs and self._completion_evidence is None:
                return "unverified_multiple_inputs"
            return "pending_prompt_result_receipts"
        if self._queued_turn_count:
            return "native_user_queue_pending"
        if self._native_state in ("running", "requires_action"):
            return "native_turn_active"
        return "completion_unverified"

    def _consume_result_inputs(self, frame: dict):
        # These contracts are declared in @anthropic-ai/claude-agent-sdk
        # 0.3.282 sdk.d.ts: SDKResultError/SDKResultSuccess and
        # SDKSessionStateChangedMessage. Result UUID arrays arrived in SDK
        # 0.3.259; queued_turn_count in 0.3.243. Feature-detect the fields,
        # without treating package versions as native protocol proof.
        if frame.get("parent_tool_use_id") is not None:
            return
        if "queued_turn_count" in frame:
            count = frame["queued_turn_count"]
            if type(count) is int and 0 <= count <= 2**53:
                self._queued_turn_count = count
            else:
                self._completion_metadata_invalid = True
                return
        explicit = "user_message_uuids" in frame or "user_message_uuid" in frame
        consumed: set[str] = set()
        if "user_message_uuids" in frame:
            identifiers = frame["user_message_uuids"]
            if not isinstance(identifiers, list) or len(identifiers) > 64 or any(_pointer(value) is None for value in identifiers):
                self._completion_metadata_invalid = True
                return
            consumed.update(identifiers)
        if "user_message_uuid" in frame:
            identifier = _pointer(frame["user_message_uuid"])
            if identifier is None or ("user_message_uuids" in frame and identifier not in consumed):
                self._completion_metadata_invalid = True
                return
            consumed.add(identifier)
        self._completion_metadata_invalid = False
        if explicit:
            matched = self._pending_inputs & consumed
            self._pending_inputs.difference_update(matched)
            if matched:
                self._completion_evidence = "result_prompt_uuid_receipts"
            return
        origin = frame.get("origin")
        attributable = origin is None or (isinstance(origin, dict) and origin.get("kind") == "human")
        if not self._overlapping_inputs and len(self._pending_inputs) == 1 and attributable:
            wire_id = next(iter(self._pending_inputs))
            request = self.requests[self._wire_requests[wire_id]]
            if request["status"] == "acknowledged":
                self._pending_inputs.clear()
                self._completion_evidence = "legacy_single_input_result"

    def _fail(self, code: str):
        if self.protocol_error is None:
            self.protocol_error = code
            self.terminal_error = True
            self.emit({"kind": "native_protocol_error", "code": code})

    def _write(self, frame: dict):
        try:
            payload = (json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise NativeProtocolError("invalid_outgoing_frame") from None
        if len(payload) - 1 > self.max_frame_bytes:
            raise NativeProtocolError("outgoing_frame_too_large")
        try:
            self.send(payload)
        except Exception:
            self._fail("write_failed_delivery_unknown")
            raise NativeProtocolError("write_failed_delivery_unknown") from None

    def _begin(self, request_id: str, action: str, wire_id: str):
        if self.closed or self.protocol_error:
            raise NativeProtocolError("session_unavailable")
        if _pointer(request_id) is None:
            raise ValueError("request_id must be a bounded non-secret identifier")
        if request_id in self.requests:
            raise NativeProtocolError("request_already_sent")
        if wire_id in self._wire_requests:
            raise NativeProtocolError("wire_request_id_already_sent")
        if len(self.requests) >= MAX_REQUESTS:
            raise NativeProtocolError("request_limit_reached")
        self.requests[request_id] = {"action": action, "status": "sent", "wire_request_id": wire_id}
        self._wire_requests[wire_id] = request_id

    def _receipt(self, request_id: str, status: str):
        record = self.requests[request_id]
        record["status"] = status
        self.emit({"kind": "native_control", "request_id": request_id, **record})

    def initialize(self, request_id: str):
        if self._initialize_sent:
            raise NativeProtocolError("initialize_already_sent")
        self._begin(request_id, "initialize", request_id)
        self._initialize_sent = True
        self._write({"type": "control_request", "request_id": request_id,
                     "request": {"subtype": "initialize", "hooks": None}})
        self._receipt(request_id, "sent")

    def send_user(self, text: str, request_id: str, session_id: str | None = None):
        if not self.initialized:
            raise NativeProtocolError("initialize_not_acknowledged")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input text must be nonempty")
        if _pointer(request_id) is None:
            raise ValueError("request_id must be a bounded non-secret identifier")
        if session_id is not None and _pointer(session_id) is None:
            raise ValueError("invalid session pointer")
        wire_id = str(uuid.uuid5(_NAMESPACE, request_id))
        # Validate size before registering a sent request; encoding failures
        # still become explicit rather than truncating or splitting a prompt.
        frame = {"type": "user", "uuid": wire_id,
                 "message": {"role": "user", "content": text},
                 "parent_tool_use_id": None,
                 "session_id": session_id or self.native_session_id or "default"}
        try:
            size = len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except UnicodeError:
            raise NativeProtocolError("invalid_outgoing_frame") from None
        if size > self.max_frame_bytes:
            raise NativeProtocolError("outgoing_frame_too_large")
        self._begin(request_id, "input", wire_id)
        self._overlapping_inputs = self._overlapping_inputs or bool(self._pending_inputs)
        self._pending_inputs.add(wire_id)
        self._completion_evidence = None
        # A prior idle marker and queue snapshot do not describe a new write.
        # If state events are enabled, require a fresh idle after this input.
        if self._native_state is not None:
            self._native_state = "running"
        self._write(frame)
        self.sent_turns += 1
        self._receipt(request_id, "sent")

    def interrupt(self, request_id: str):
        if not self.initialized:
            raise NativeProtocolError("initialize_not_acknowledged")
        self._begin(request_id, "interrupt", request_id)
        self._write({"type": "control_request", "request_id": request_id,
                     "request": {"subtype": "interrupt"}})
        self._receipt(request_id, "sent")

    def expire(self, request_id: str):
        """Record missing acknowledgment; never resend an uncertain request."""
        record = self.requests.get(request_id)
        if record and record["status"] == "sent":
            self._receipt(request_id, "unacknowledged")
            if record["action"] == "initialize":
                self._fail("initialize_not_acknowledged")

    def feed(self, chunk: bytes):
        """Accept arbitrarily split NDJSON without an unbounded readline."""
        if self.closed:
            raise NativeProtocolError("session_closed")
        if not isinstance(chunk, bytes):
            raise TypeError("feed requires bytes")
        if self.protocol_error:
            return
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline
            if len(self._buffer) + end - offset > self.max_frame_bytes:
                self._buffer.clear()
                self._fail("incoming_frame_too_large")
                return
            self._buffer.extend(chunk[offset:end])
            if newline < 0:
                return
            line = bytes(self._buffer)
            self._buffer.clear()
            offset = newline + 1
            if not line.strip():
                continue
            try:
                frame = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                self._fail("invalid_json_frame")
                return
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                self._fail("invalid_frame_shape")
                return
            self._handle(frame)
            if self.protocol_error:
                return

    def _handle(self, frame: dict):
        kind = frame["type"]
        if kind == "control_response":
            response = frame.get("response")
            if not isinstance(response, dict):
                self._fail("invalid_control_response")
                return
            wire_id = response.get("request_id")
            request_id = self._wire_requests.get(wire_id) if isinstance(wire_id, str) else None
            if request_id is None:
                return  # Unrelated/late responses cannot acknowledge our input.
            record = self.requests[request_id]
            if record["action"] == "input" or record["status"] != "sent":
                return
            if response.get("subtype") not in ("success", "error"):
                self._fail("invalid_control_response")
                return
            success = response["subtype"] == "success"
            if success and response.get("response") is not None and not isinstance(response["response"], dict):
                self._fail("invalid_control_response")
                return
            self._receipt(request_id, "acknowledged" if success else "rejected")
            if record["action"] == "initialize":
                if success:
                    self.initialized = True
                else:
                    self._fail("initialize_rejected")
            return
        if kind == "control_request":
            self._decision(frame)
            return
        if kind == "user" and frame.get("parent_tool_use_id") is None:
            wire_id = frame.get("uuid")
            request_id = self._wire_requests.get(wire_id) if isinstance(wire_id, str) else None
            if request_id and self.requests[request_id]["action"] == "input" and self.requests[request_id]["status"] == "sent":
                self._receipt(request_id, "acknowledged")
        if pointer := _pointer(frame.get("session_id")):
            self.native_session_id = pointer
        if kind == "system":
            if frame.get("subtype") == "session_state_changed" and frame.get("state") in ("idle", "running", "requires_action"):
                self._native_state = frame["state"]
            task_id = _pointer(frame.get("task_id"))
            if task_id and frame.get("subtype") == "task_started" and frame.get("task_type") in ("local_agent", "local_workflow"):
                if len(self.active_tasks) >= MAX_REQUESTS and task_id not in self.active_tasks:
                    self._fail("active_task_limit_reached")
                    return
                self.active_tasks.add(task_id)
            elif task_id and frame.get("subtype") == "task_notification":
                self.active_tasks.discard(task_id)
            elif task_id and frame.get("subtype") == "task_updated":
                patch = frame.get("patch")
                if isinstance(patch, dict) and isinstance(patch.get("status"), str) and patch["status"] in _TERMINAL_TASKS:
                    self.active_tasks.discard(task_id)
        if kind == "result":
            result_id = _pointer(frame.get("uuid"))
            if result_id is None or result_id not in self._results:
                if result_id:
                    if len(self._results) >= MAX_REQUESTS:
                        self._fail("result_limit_reached")
                        return
                    self._results.add(result_id)
                origin = frame.get("origin")
                if origin is None or (isinstance(origin, dict) and origin.get("kind") == "human"):
                    self.completed_turns += 1
                self._consume_result_inputs(frame)
                interrupted = frame.get("terminal_reason") in ("aborted_streaming", "aborted_tools")
                self.last_result_success = frame.get("subtype") == "success" and frame.get("is_error") is False and not interrupted
                if not self.last_result_success:
                    self.terminal_error = True
        for event in normalize_event(frame):
            self.emit(event)

    def _decision(self, frame: dict):
        request_id = _pointer(frame.get("request_id"))
        request = frame.get("request")
        if request_id is None or not isinstance(request, dict):
            self._fail("invalid_native_control_request")
            return
        subtype = request.get("subtype")
        if subtype == "can_use_tool":
            response = {"type": "control_response", "response": {
                "subtype": "success", "request_id": request_id,
                "response": {"behavior": "deny", "interrupt": True,
                             "message": "Agent Workbench requires an interactive native permission decision; resume explicitly in the native harness."}}}
            decision_type = "permission"
        else:
            response = {"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": "Agent Workbench has no registered callback for this native request."}}
            decision_type = "hook" if subtype == "hook_callback" else "unsupported"
        self._write(response)
        self.emit({"kind": "native_decision_required", "decision_type": decision_type,
                   "request_id": request_id})
        self._fail("native_decision_required")

    def finish(self):
        """Mark EOF and unresolved receipts without claiming non-delivery."""
        if self.closed:
            return
        if self._buffer and not self.protocol_error:
            self._fail("incomplete_json_frame")
        self._buffer.clear()
        for request_id in list(self.requests):
            self.expire(request_id)
        self.closed = True
