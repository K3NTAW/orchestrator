"""Notifications with persistent transition deduplication."""
import os, subprocess, sys, urllib.request
from . import bus

def _deliver(msg):
    print(f"[notify] {msg}", file=sys.stderr)
    url = os.environ.get("ORCH_NOTIFY_URL")
    if url:
        try:
            # msg is untrusted (merge stderr, task titles): capped the same as the osascript arm below so an
            # unbounded blob of git output is never shipped whole to an external webhook.
            req = urllib.request.Request(url, data=msg[:200].encode(), method="POST",
                                          headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            print(f"[notify] webhook failed: {e}", file=sys.stderr)
    if sys.platform == "darwin" and os.environ.get("ORCH_NOTIFY_DESKTOP") != "0":
        # msg is untrusted (merge stderr, task titles): passed as an argv item, never interpolated into the
        # AppleScript source, so a quote in it cannot break out and run arbitrary local commands.
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display notification (item 1 of argv) with title "orchestrator"', "-e", "end run",
                        "--", msg[:200]], check=False)

# The pipeline installs its public facade for backwards-compatible notification hooks.
_sink = _deliver


def notify(msg):
    _sink(msg)


def notify_once(task_id, transition, msg):
    with bus.locked():
        task = bus.get(task_id)
        pipeline = dict(task.get("pipeline") or {})
        if pipeline.get("notification_transition") == transition:
            return False
        pipeline["notification_transition"] = transition
        bus.update(task_id, pipeline=pipeline)
    notify(msg)
    return True
