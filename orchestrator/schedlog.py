"""Append-only scheduler telemetry, tolerant of interrupted or malformed writes."""
import json
import time

from . import STATE

SCHED_DIR = STATE / "runs" / "sched"


def append(name, row):
    record = dict(row)
    record.setdefault("ts", time.time())
    SCHED_DIR.mkdir(parents=True, exist_ok=True)
    with (SCHED_DIR / f"{name}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def read_with_malformed(name):
    """Return (dict rows, malformed count); blank lines are ignored."""
    rows, malformed = [], 0
    try:
        stream = (SCHED_DIR / f"{name}.jsonl").open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return rows, malformed
    with stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                malformed += 1
    return rows, malformed


def read(name):
    return read_with_malformed(name)[0]
