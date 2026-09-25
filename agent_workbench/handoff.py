"""Portable task handoffs. Digests detect changes; imported claims confer no trust."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import stat

from .store import add_event, now, validate_manifest


FORMAT = "agent-workbench-handoff"
MAX_BYTES = 2_000_000
MAX_TASKS = 200
MAX_NOTES = 500
TASK_FIELDS = {"id", "harness", "depends_on", "prompt", "argv", "model", "effort", "timeout_seconds"}
TASK_STATUSES = {"pending", "running", "succeeded", "failed", "cancelled", "interrupted"}
RUN_STATUSES = {"ready", "running", "paused", "stopped", "succeeded", "failed", "interrupted"}
NOTE_KINDS = {"decision", "question", "evidence", "next"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----"),
    re.compile(r"\bsk-(?:ant-[A-Za-z0-9_-]+|[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?\s*(?:bearer|basic)\s+[^\s\"']{6,}"),
    re.compile(r"(?i)\b[a-z0-9_-]*(?:api[_-]?key|token|password|secret(?:[_-](?:access[_-])?key)?)"
               r"[\"']?\s*[:=]\s*[\"']?[^\s\"',;}{]{4,}"),
    re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s@]+:[^/\s@]+@"),
)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("Invalid handoff object fields.")
    return value


def _text(value, *, limit=16384, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("Invalid or oversized handoff text.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ValueError("Handoff text contains control characters.")
    return value


def _identifier(value):
    _text(value, limit=128)
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid handoff identifier.")
    return value


def _integer(value, *, maximum=1_000_000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid handoff integer.")
    return value


def contains_credential_text(text: str) -> bool:
    """Shared recognizable-credential check for handoffs and automatic context."""
    inspected = re.sub(r"(?i)(?:\[redacted\]|<redacted>)", "", text)
    return any(pattern.search(inspected) for pattern in SECRET_PATTERNS)


def _check_secrets(value):
    if isinstance(value, str):
        if contains_credential_text(value):
            raise ValueError("Handoff contains credential-like text; remove or redact it before export or import.")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_secrets(key)
            _check_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _check_secrets(item)


def _canonical(document):
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Handoff is not bounded UTF-8 JSON data.") from None


def _digest(document):
    unsigned = {key: value for key, value in document.items() if key != "digest"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _validate(document):
    _object(document, {"format", "version", "exported_at", "source", "manifest", "history", "notes", "digest"})
    if document["format"] != FORMAT or type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("Unsupported handoff format or version.")
    _text(document["exported_at"], limit=64)
    source = _object(document["source"], {"run_id", "created_at", "updated_at", "revision"})
    _identifier(source["run_id"])
    _integer(source["revision"])
    for field in ("created_at", "updated_at"):
        _text(source[field], limit=64)
    manifest = _object(document["manifest"], {"version", "name", "goal", "tasks"})
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("Unsupported handoff manifest version.")
    _text(manifest["name"], limit=200)
    _text(manifest["goal"], limit=65536)
    tasks = manifest["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("Handoffs require between 1 and 200 tasks.")
    for task in tasks:
        _object(task, {"id", "harness", "depends_on"}, TASK_FIELDS - {"id", "harness", "depends_on"})
        _identifier(task["id"])
        _text(task["harness"], limit=32)
        if not isinstance(task["depends_on"], list) or len(task["depends_on"]) > MAX_TASKS:
            raise ValueError("Invalid handoff dependencies.")
        for dependency in task["depends_on"]:
            _identifier(dependency)
        for field in ("model", "effort"):
            if field in task:
                _text(task[field], limit=200)
        if "prompt" in task:
            _text(task["prompt"], limit=100000)
        if "argv" in task:
            argv = task["argv"]
            if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
                raise ValueError("Invalid handoff command arguments.")
            for arg in argv:
                _text(arg, limit=16000, empty=True)
    # Store remains the owner of execution semantics, allowed settings and DAG rules.
    validate_manifest(deepcopy(manifest))
    history = _object(document["history"], {"run_status", "tasks"})
    if history["run_status"] not in RUN_STATUSES:
        raise ValueError("Invalid historical run status.")
    historical_tasks = history["tasks"]
    if not isinstance(historical_tasks, list) or len(historical_tasks) != len(tasks):
        raise ValueError("Historical task claims must match the manifest.")
    ids = set()
    for claim in historical_tasks:
        _object(claim, {"id", "status", "attempt_count"})
        _identifier(claim["id"])
        if claim["status"] not in TASK_STATUSES or claim["id"] in ids:
            raise ValueError("Invalid or duplicate historical task claim.")
        ids.add(claim["id"])
        _integer(claim["attempt_count"])
    if ids != {task["id"] for task in tasks}:
        raise ValueError("Historical task claims must match the manifest.")
    notes = document["notes"]
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        raise ValueError("Handoff contains too many notes.")
    for note in notes:
        _object(note, {"kind", "text", "at"})
        if note["kind"] not in NOTE_KINDS:
            raise ValueError("Invalid handoff note kind.")
        _text(note["text"])
        _text(note["at"], limit=64)
    digest = _object(document["digest"], {"algorithm", "value"})
    if digest["algorithm"] != "sha256" or not isinstance(digest["value"], str) or not re.fullmatch(r"[a-f0-9]{64}", digest["value"]):
        raise ValueError("Invalid handoff digest.")
    if not hmac.compare_digest(digest["value"], _digest(document)):
        raise ValueError("Handoff digest mismatch: artifact changed or is incomplete.")
    _check_secrets(document)
    if len(_canonical(document)) > MAX_BYTES:
        raise ValueError("Handoff exceeds the 2 MB size limit.")
    return document


def _markdown(document):
    # Quote user text as data, escaping raw HTML and Markdown punctuation.
    def quoted(text):
        escaped = html.escape(text, quote=False)
        escaped = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escaped)
        return "\n".join("> " + line for line in escaped.splitlines())

    manifest = document["manifest"]
    lines = ["# Portable agent handoff", "", quoted(manifest["name"]), "", "## Goal", "", quoted(manifest["goal"]), "",
             "## Historical task claims", "", "These statuses describe the source run. Import resets all runnable tasks to pending.", "",
             f"Source run: `{document['source']['run_id']}`; reported status: `{document['history']['run_status']}`.", ""]
    claims = {claim["id"]: claim for claim in document["history"]["tasks"]}
    for task in manifest["tasks"]:
        dependencies = ", ".join(task["depends_on"]) or "none"
        lines.append(f"- `{task['id']}` — `{claims[task['id']]['status']}`; harness `{task['harness']}`; dependencies: {dependencies}.")
    for kind, title in (("decision", "Decisions"), ("question", "Open questions"), ("evidence", "Explicit evidence"), ("next", "Next steps")):
        lines.extend(["", "## " + title, ""])
        notes = [note for note in document["notes"] if note["kind"] == kind]
        if not notes:
            lines.append("None recorded.")
        for note in notes:
            lines.extend([quoted(note["text"]), ""])
    lines.extend(["", "## Artifact integrity", "", f"SHA-256: `{document['digest']['value']}`.", "",
                  "The digest detects accidental changes to the JSON content. It is not a signature, proof of authorship, or verification of any claim.",
                  "JSON is authoritative; this Markdown is a generated summary. Import reads JSON only.", ""])
    return "\n".join(lines).encode("utf-8")


def _write_new(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def export_handoff(store, run_id, output: Path) -> dict:
    """Export selected portable data, refusing to overwrite either output file."""
    output = Path(output)
    if output.suffix.lower() != ".json":
        raise ValueError("Handoff output must use a .json filename.")
    markdown = output.with_suffix(".md")
    if output.exists() or output.is_symlink() or markdown.exists() or markdown.is_symlink():
        raise FileExistsError("Handoff JSON or Markdown output already exists; choose a new filename.")
    state = store.read(run_id)
    tasks = [{key: deepcopy(value) for key, value in task.items() if key in TASK_FIELDS} for task in state["tasks"]]
    for task in tasks:
        task.setdefault("depends_on", [])
    document = {
        "format": FORMAT, "version": 1, "exported_at": now(),
        "source": {"run_id": state["id"], "created_at": state["created_at"], "updated_at": state["updated_at"], "revision": state["revision"]},
        "manifest": {"version": 1, "name": state["name"], "goal": state["goal"], "tasks": tasks},
        "history": {"run_status": state["status"], "tasks": [
            {"id": task["id"], "status": task["status"], "attempt_count": len(task.get("attempts", []))} for task in state["tasks"]]},
        "notes": deepcopy(state["notes"]),
    }
    document["digest"] = {"algorithm": "sha256", "value": _digest(document)}
    _validate(document)
    encoded = _canonical(document) + b"\n"
    if len(encoded) > MAX_BYTES:
        raise ValueError("Handoff exceeds the 2 MB size limit.")
    _write_new(output, encoded)
    try:
        _write_new(markdown, _markdown(document))
    except BaseException:
        # Only roll back the new JSON created by this call, never pre-existing files.
        output.unlink(missing_ok=True)
        raise
    return document


def _read_document(source):
    # Nonblocking open permits rejecting FIFOs before a peer can stall the import.
    fd = os.open(Path(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Handoff source must be a regular JSON file.")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Handoff exceeds the 2 MB size limit.")

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON fields are not allowed in handoffs.")
            result[key] = value
        return result

    def no_constants(_value):
        raise ValueError("Non-finite JSON numbers are not allowed in handoffs.")

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constants)
        _validate(document)
    except (TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("Invalid handoff structure or encoding.") from None
    return document, raw


def import_handoff(store, source: Path, project: Path) -> dict:
    """Create a fresh, unexecuted run; retain all imported outcomes as history only."""
    document, raw = _read_document(source)
    state = store.create(deepcopy(document["manifest"]), Path(project))
    artifact = "handoff-source.json"
    _write_new(store.directory(state["id"]) / artifact, raw)

    def imported(current):
        current["notes"] = deepcopy(document["notes"])
        current["handoff"] = {
            "version": 1, "imported_at": now(), "source": deepcopy(document["source"]),
            "source_digest": deepcopy(document["digest"]), "source_artifact": artifact,
            "historical": deepcopy(document["history"]),
        }
        add_event(current, "handoff_imported", source_run_id=document["source"]["run_id"], digest=document["digest"]["value"])

    return store.update(state["id"], imported)
