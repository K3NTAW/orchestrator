"""Read the current Planner transcript's context usage for the prompt hook."""
import json
from pathlib import Path

from .pool import encode_project_dir


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
    directory = config_dir / "projects" / encode_project_dir(str(root))
    if transcript is not None:
        transcript = Path(transcript)
        if not transcript.is_file():
            return None
    elif session_id is not None:
        transcript = directory / f"{session_id}.jsonl"
        if not transcript.is_file():
            return None
    else:
        try:
            transcript = max(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
        except (FileNotFoundError, ValueError):
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


def hook_message(cfg, config_dir: Path, root: Path, *, transcript=None, session_id=None) -> str | None:
    """Return the one-line handover instruction when the configured limit is reached."""
    tokens = context_tokens(config_dir, root, transcript=transcript, session_id=session_id)
    threshold = cfg.get("planner", {}).get("handover_context_tokens")
    if tokens is None or not isinstance(threshold, int) or tokens < threshold:
        return None
    return (f"Planner context is {tokens} tokens (threshold {threshold}); hand over now: "
            "uv run orchestrator handover --reason context")
