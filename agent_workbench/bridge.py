"""Durable public event receipts and explicitly addressed native controls."""
from __future__ import annotations

import json
import re
import time
import uuid

from .store import add_event, now


MAX_NATIVE_EVENTS = 2000
FLUSH_EVENTS = 100
FLUSH_SECONDS = 0.25
_FIELDS = {"kind", "status", "action", "request_id", "wire_request_id", "session_id",
           "task_id", "tool_use_id", "parent_tool_use_id", "item_id", "agent_id", "tool_name",
           "code", "decision_type", "subtype", "tool", "role", "terminal_reason", "input_tokens", "output_tokens",
           "cache_read_input_tokens", "cache_creation_input_tokens", "total_cost_usd",
           "num_turns", "duration_ms", "duration_api_ms", "usage", "task_type"}


class PublicJournal:
    def __init__(self, store, run_id, task_id, attempt_id):
        self.store, self.run_id, self.task_id, self.attempt_id = store, run_id, task_id, attempt_id
        self.pending = []
        self.last_flush = time.monotonic()

    def emit(self, event):
        # NativeSession already normalizes; this second allowlist makes the
        # state boundary independent of transport implementation details.
        selected = {key: value for key, value in event.items() if key in _FIELDS}
        if "task_id" in selected:
            selected["native_task_id"] = selected.pop("task_id")
        selected.update(at=now(), task_id=self.task_id, attempt_id=self.attempt_id)
        self.pending.append(selected)
        self.flush(force=False)

    def flush(self, force=True):
        """Persist pending events; unforced calls batch rewrites of the run state.

        Pollers call ``flush(force=False)`` every cycle. Lifecycle boundaries
        force a flush so receipts are durable before outcomes are recorded.
        """
        if not self.pending:
            return
        if not force and len(self.pending) < FLUSH_EVENTS and time.monotonic() - self.last_flush < FLUSH_SECONDS:
            return
        batch, self.pending = self.pending, []
        def record(state):
            task = next(t for t in state["tasks"] if t["id"] == self.task_id)
            attempt = next(a for a in task["attempts"] if a["id"] == self.attempt_id)
            receipts = attempt.setdefault("control_receipts", [])
            for event in batch:
                state["events"].append(event)
                if event.get("session_id"):
                    attempt["native_session_id"] = event["session_id"]
                if event.get("kind") == "native_control":
                    receipts.append({key: event[key] for key in ("at", "request_id", "action", "status") if key in event})
                    del receipts[:-64]
                    if event.get("action") == "initialize" and event.get("status") == "acknowledged":
                        attempt["duplex_initialized"] = True
                    for request in state["control"].get("native_requests", []):
                        if request["id"] == event.get("request_id") and request["attempt_id"] == self.attempt_id:
                            request.update(status=event["status"], updated_at=event["at"])
                if isinstance(event.get("usage"), dict):
                    attempt["usage"] = event["usage"]
                if isinstance(event.get("total_cost_usd"), (int, float)):
                    attempt["total_cost_usd"] = event["total_cost_usd"]
            native = [i for i, event in enumerate(state["events"]) if str(event.get("kind", "")).startswith("native_")]
            remove = set(native[:-MAX_NATIVE_EVENTS])
            if remove:
                state["events"] = [event for i, event in enumerate(state["events"]) if i not in remove]
                state["native_events_dropped"] = state.get("native_events_dropped", 0) + len(remove)
        self.store.update(self.run_id, record)
        self.last_flush = time.monotonic()


class EventTail:
    """Incrementally read public metadata from a private, growing NDJSON log."""
    def __init__(self, path, harness, emit, limit=2_097_152):
        self.path, self.harness, self.emit, self.limit = path, harness, emit, limit
        self.offset, self.buffer, self.dropping = 0, b"", False
        self.last_kind = None

    def poll(self, final=False):
        from .native import normalize_event
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            raw = stream.read(self.limit)
            self.offset = stream.tell()
        self.buffer += raw
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if self.dropping:
                self.dropping = False
                continue
            if len(line) > self.limit:
                continue
            self._line(line, normalize_event)
        if len(self.buffer) > self.limit:
            self.buffer = b""
            self.dropping = True
        if final and not raw and self.buffer and not self.dropping:
            self._line(self.buffer, normalize_event)
            self.buffer = b""
        return bool(raw)

    def _line(self, line, normalize_event):
        try:
            frame = json.loads(line)
        except (ValueError, UnicodeError):
            return
        for event in normalize_event(frame, harness=self.harness):
            # Hermes streams each text delta as a frame. One public message
            # event per contiguous run keeps tool events within the journal cap.
            if (self.harness in {"hermes", "hermes-astra-ultra"} and event.get("kind") == "native_message"
                    and self.last_kind == "native_message"):
                continue
            self.last_kind = event.get("kind")
            self.emit(event)


def request_native(store, run_id, task_id, action, text=None, request_id=None):
    if action not in {"input", "interrupt"}:
        raise ValueError("Native control must be input or interrupt")
    if action == "input" and (not isinstance(text, str) or not text.strip() or len(text) > 16000 or "\0" in text):
        raise ValueError("Native input must contain 1–16000 characters")
    ident = uuid.uuid4().hex if request_id is None else request_id
    from .native import _pointer
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", ident) or _pointer(ident) is None:
        raise ValueError("Invalid native request ID")
    def queue(state):
        requests = state["control"].setdefault("native_requests", [])
        existing = next((r for r in requests if r["id"] == ident), None)
        if existing:
            if existing["task_id"] != task_id or existing["action"] != action or existing.get("text") != text:
                raise ValueError("Request ID already identifies different input")
            return
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        if not task or task["status"] != "running" or task.get("adapter") != "duplex":
            raise ValueError("Address a running task with the duplex adapter; use steer for next-task guidance")
        attempt = task["attempts"][-1]
        if not attempt.get("duplex_initialized") or attempt.get("input_closed"):
            raise ValueError("Native duplex input is not open and initialized")
        # Keep controller-generated IDs in a reserved namespace, including
        # future suffixes and attempts that have not yet been created. Existing
        # records were handled above so a repeated caller ID remains a lookup,
        # never a dispatch into a newer attempt.
        attempt_ids = [entry["id"] for candidate in state["tasks"] for entry in candidate.get("attempts", [])]
        if (ident.startswith("workbench-") or re.match(r"[a-f0-9]{32}-", ident)
                or any(ident.startswith(attempt_id + "-") for attempt_id in attempt_ids)):
            raise ValueError("Native request ID uses a reserved controller namespace")
        # User frames carry deterministic UUIDs while interrupt controls use
        # their caller ID. Reject cross-kind wire collisions before accepting
        # a durable request, rather than aborting an otherwise healthy session
        # later in NativeSession._begin.
        from .native import _NAMESPACE
        wire_id = str(uuid.uuid5(_NAMESPACE, ident)) if action == "input" else ident
        occupied = {attempt["id"] + "-initialize", str(uuid.uuid5(_NAMESPACE, attempt["id"] + "-initial"))}
        occupied.update(str(uuid.uuid5(_NAMESPACE, request["id"])) if request["action"] == "input" else request["id"]
                        for request in requests if request["attempt_id"] == attempt["id"])
        if wire_id in occupied:
            raise ValueError("Native request ID collides with an existing wire identifier")
        if len(requests) >= 128 or sum(r["status"] == "queued" for r in requests) >= 64:
            raise ValueError("Native control queue limit reached")
        # A native result may coalesce every input into one UUID batch. Keep
        # that batch within the parser's bound, including the initial prompt.
        if action == "input" and sum(r["attempt_id"] == attempt["id"] and r["action"] == "input"
                                     for r in requests) >= 63:
            raise ValueError("Native input limit reached for this attempt")
        entry = {"id": ident, "task_id": task_id, "attempt_id": attempt["id"], "action": action,
                 "status": "queued", "at": now()}
        if text is not None:
            entry["text"] = text
        requests.append(entry)
        add_event(state, "native_control_queued", task_id=task_id, attempt_id=attempt["id"], request_id=ident, action=action, status="queued")
    result = store.update(run_id, queue)
    record = next(r for r in result["control"]["native_requests"] if r["id"] == ident)
    return {key: value for key, value in record.items() if key != "text"}


def claim_requests(store, run_id, attempt_id):
    if not any(r["attempt_id"] == attempt_id and r["status"] == "queued"
               for r in store.read(run_id)["control"].get("native_requests", [])):
        return []
    claimed = []
    def claim(state):
        for request in state["control"].get("native_requests", []):
            if request["attempt_id"] == attempt_id and request["status"] == "queued":
                request.update(status="dispatching", updated_at=now())
                claimed.append(dict(request))
    store.update(run_id, claim)
    return claimed


def close_requests(store, run_id, attempt_id):
    store.update(run_id, lambda state: mark_closed(state, attempt_id))


def mark_closed(state, attempt_id):
    for task in state["tasks"]:
        for attempt in task.get("attempts", []):
            if attempt["id"] == attempt_id:
                attempt["input_closed"] = True
    for request in state["control"].get("native_requests", []):
        if request["attempt_id"] == attempt_id and request["status"] in {"queued", "dispatching", "sent"}:
            request.update(status="not_sent" if request["status"] == "queued" else "unacknowledged", updated_at=now())
