"""Read the current Planner transcript's context usage for the prompt hook."""
import json
import re
from pathlib import Path

from .pool import encode_project_dir


DEFAULT_HANDOVER_CONTEXT_TOKENS = 300000
DEFAULT_PLAN_MAX_CHARS = 12000
COMPACT_HEADER = ("Session compacted; continue in place. Planner mode: goals go through "
                  "Skill(orchestrate); never edit source; see CLAUDE.md.")
COMPACT_FOOTER = ("Full state: .orchestrator/plan.md; history: "
                  ".orchestrator/plan-log.md (do not read by default).")
_HANDOVER_HEADING_RE = re.compile(r"(?m)^## Auto-handover ")
_HANDOVER_END_RE = re.compile(r"(?m)^<!-- end auto-handover -->$")


def _plan_without_handover(plan):
    matches = list(_HANDOVER_HEADING_RE.finditer(plan))
    if not matches:
        return plan
    heading = matches[-1]
    marker = _HANDOVER_END_RE.search(plan, heading.end())
    end = marker.end() if marker else len(plan)
    return plan[:heading.start()] + plan[end:]


def compact_brief(root, limit=4000) -> str:
    """Return the current Planner state suitable for a post-compaction hook."""
    path = Path(root) / ".orchestrator" / "plan.md"
    try:
        plan = path.read_text()
    except (FileNotFoundError, OSError, UnicodeError):
        return f"{COMPACT_HEADER}\nplan.md missing; run the resume skill."

    lines = plan.splitlines(keepends=True)
    now = next((index for index, line in enumerate(lines) if line.startswith("## Now")), None)
    if now is not None:
        end = next((index for index in range(now + 1, len(lines))
                    if lines[index].startswith("## ")), len(lines))
        text = "".join(lines[now:end])
    else:
        text = plan
    if len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        cut = cut if cut >= 0 else limit
        text = text[:cut].rstrip("\n") + "\n[cut; read .orchestrator/plan.md]"
    return f"{COMPACT_HEADER}\n{text}\n{COMPACT_FOOTER}"


def _transcript_path(config_dir: Path, root: Path, *, transcript=None, session_id=None):
    directory = config_dir / "projects" / encode_project_dir(str(root))
    if transcript is not None:
        path = Path(transcript)
        return path if path.is_file() else None
    if session_id is not None:
        path = directory / f"{session_id}.jsonl"
        return path if path.is_file() else None
    try:
        return max(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    except (FileNotFoundError, ValueError):
        return None


def _reverse_lines(path):
    """Yield a text file's lines last-first without loading its transcript into memory."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        remainder = b""
        while position:
            size = min(8192, position)
            position -= size
            stream.seek(position)
            chunks = (stream.read(size) + remainder).split(b"\n")
            remainder = chunks[0]
            for line in reversed(chunks[1:]):
                yield line.decode("utf-8", errors="replace")
        if remainder:
            yield remainder.decode("utf-8", errors="replace")


def context_tokens(config_dir: Path, root: Path, *, transcript=None, session_id=None) -> int | None:
    """Return a transcript's final recorded input-context size, if present."""
    transcript = _transcript_path(config_dir, root, transcript=transcript, session_id=session_id)
    if transcript is None:
        return None
    for line in _reverse_lines(transcript):
        try:
            message = json.loads(line).get("message", {})
        except json.JSONDecodeError:
            continue
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            return sum(usage.get(key, 0) for key in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return None


def user_turns(transcript) -> int:
    """Count user prompt records, excluding user records that contain tool results."""
    try:
        stream = _reverse_lines(Path(transcript))
        count = 0
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "user":
                continue
            content = (record.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
                continue
            if isinstance(content, (str, list)):
                count += 1
        return count
    except (OSError, TypeError, AttributeError):
        return 0


def _last_handover(root):
    path = Path(root) / ".orchestrator" / "checkpoint" / "handover-session.json"
    try:
        record = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return record if isinstance(record, dict) else None


def hook_message(cfg, config_dir: Path, root: Path, *, transcript=None, session_id=None) -> str | None:
    """Return the one-line handover instruction when the configured limit is reached."""
    tokens = context_tokens(config_dir, root, transcript=transcript, session_id=session_id)
    planner = cfg.get("planner", {})
    threshold = planner.get("handover_context_tokens", DEFAULT_HANDOVER_CONTEXT_TOKENS)
    messages = []
    plan_max = planner.get("plan_max_chars", DEFAULT_PLAN_MAX_CHARS)
    plan_path = Path(root) / ".orchestrator" / "plan.md"
    try:
        plan_chars = len(_plan_without_handover(plan_path.read_text()))
    except (FileNotFoundError, OSError, UnicodeError):
        plan_chars = None
    if isinstance(plan_max, int) and plan_max > 0 and plan_chars is not None and plan_chars > plan_max:
        messages.append(f"plan.md is {plan_chars} chars (max {plan_max}): move history to "
                        ".orchestrator/plan-log.md and keep ## Now current.")

    if tokens is None or not isinstance(threshold, int) or threshold == 0 or tokens < threshold:
        return "\n".join(messages) or None
    if transcript is not None or session_id is not None:
        grace = planner.get("handover_grace_turns", 12)
        if isinstance(grace, int) and user_turns(_transcript_path(
                config_dir, root, transcript=transcript, session_id=session_id)) < grace:
            return "\n".join(messages) or None
    record = _last_handover(root)
    if session_id is not None and record and record.get("session_id") == session_id:
        recorded_tokens = record.get("tokens_at")
        if isinstance(recorded_tokens, int) and tokens - recorded_tokens <= planner.get("handover_regrow_tokens", 20000):
            stamp = record.get("ts")
            from datetime import datetime
            when = datetime.fromtimestamp(stamp).strftime("%H:%M") if isinstance(stamp, (int, float)) else "??:??"
            messages.append(f"handover written {when}; the session compacts in place (auto-compact or /compact), no restart needed")
            return "\n".join(messages)
    command = "uv run orchestrator handover --reason context"
    if session_id is not None:
        command += f" --session-id {session_id}"
    messages.append(f"Planner context is {tokens} tokens (threshold {threshold}); write the checkpoint: {command}; "
                    "then keep working, the session compacts in place (auto-compact or /compact).")
    return "\n".join(messages)
