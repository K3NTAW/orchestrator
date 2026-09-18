"""orchestrator.serve: the per-user goal endpoint that kgpt's modules/orchestrator (Phase C) talks to over HTTP.
Runs in the executor container on the home network. Forwards start/status/list/cancel to orchestrator.goals
against whichever repo a request names (by slug, resolved through .orchestrator/repos.toml) -- it never touches
goals.py internals directly, only the module's public functions, so goals.py's own contract (list-of-dicts,
{"error": "unknown goal"}, {"launched": bool, ...}) is the single source of truth for what a response contains.

Requester scoping is out of scope here by design: the bearer token authenticates one kgpt connection, not an
individual end user. kgpt is responsible for scoping which users may act before it forwards a request here.
"""
import fcntl, hmac, json, logging, os, re, subprocess, sys, tomllib
from pathlib import Path

try:
    import anyio
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
except ImportError as e:
    print(f"serve: starlette/uvicorn missing: they ship with the mcp package this project depends on; "
          f"reinstall with uv sync ({e})", file=sys.stderr)
    sys.exit(1)

from . import goals

PACKAGE_REPO = Path(__file__).resolve().parents[1]
CLONE_TIMEOUT_S = int(os.environ.get("ORCH_CLONE_TIMEOUT_S") or 300)
MAX_BODY_BYTES = 65536  # reject oversized request bodies with 413 before parsing JSON
MAX_GOAL_CHARS = 20000  # goal ends up in argv of the Planner launch; longer goals hit E2BIG otherwise

# Redacts a credential embedded in a git URL (https://user:token@host/...) before anything reaches the log --
# clone stderr is never safe to echo verbatim, and it must never reach a response body at all (T-0146 risk 3).
CREDENTIAL_RE = re.compile(r"://[^/@\s]+@")

access_log = logging.getLogger("orchestrator.serve.access")


def _work_dir():
    return Path(os.environ.get("ORCH_WORK") or "/work")


def _repos_toml_path():
    return Path(os.environ.get("ORCH_REPOS_TOML", str(PACKAGE_REPO / ".orchestrator" / "repos.toml")))


def _load_repos():
    p = _repos_toml_path()
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text())


def _resolve_repo(slug):
    """(cfg, None) when usable, else (None, error JSONResponse). Distinguishes an unknown slug (404 "unknown
    repo") from a configured-but-incomplete entry missing "path" (404 "repo not configured", never a 500)."""
    repos = _load_repos()
    if slug not in repos:
        return None, JSONResponse({"reason": "unknown repo"}, status_code=404)
    cfg = repos[slug]
    if "path" not in cfg:
        return None, JSONResponse({"reason": "repo not configured"}, status_code=404)
    return cfg, None


def _lock_path(slug):
    d = _work_dir() / ".locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slug}.lock"


def _with_repo_lock(slug, fn):
    lock_path = _lock_path(slug)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _auth_error(request):
    token = os.environ.get("ORCH_SERVICE_TOKEN", "")
    header = request.headers.get("authorization", "")
    provided = header[len("Bearer "):] if header.startswith("Bearer ") else ""
    try:
        # Headers arrive latin-1 decoded; a non-ASCII value must not turn into a 500 here.
        match = bool(provided) and hmac.compare_digest(
            provided.encode("utf-8", "surrogateescape"), token.encode("utf-8", "surrogateescape")
        )
    except Exception:
        match = False
    if not match:
        return JSONResponse({"reason": "unauthorized"}, status_code=401)
    return None


def healthz(request):
    return JSONResponse({"status": "ok"}, status_code=200)


def create_goal(request):
    auth_err = _auth_error(request)
    if auth_err:
        return auth_err

    raw = anyio.from_thread.run(request.body)
    if len(raw) > MAX_BODY_BYTES:
        return JSONResponse({"reason": "body too large"}, status_code=413)
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return JSONResponse({"reason": "invalid json body"}, status_code=400)

    repo = body.get("repo")
    goal = body.get("goal")
    requester = body.get("requester")
    account = body.get("account", "A")

    if not isinstance(repo, str) or not repo:
        return JSONResponse({"reason": "repo is required"}, status_code=400)
    if not isinstance(goal, str) or not goal.strip():
        return JSONResponse({"reason": "goal is required"}, status_code=400)
    if len(goal) > MAX_GOAL_CHARS:
        return JSONResponse({"reason": "goal too long"}, status_code=400)
    if requester is not None and not isinstance(requester, str):
        return JSONResponse({"reason": "requester must be a string"}, status_code=400)
    if not isinstance(account, str):
        return JSONResponse({"reason": "account must be a string"}, status_code=400)

    cfg, err = _resolve_repo(repo)
    if err:
        return err
    path = cfg["path"]

    def run_under_lock():
        if not os.path.isdir(path):
            git_url = cfg.get("git_url")
            if not git_url:
                return JSONResponse({"reason": "repo path missing and no git_url"}, status_code=404)
            try:
                r = subprocess.run(["git", "clone", git_url, path], capture_output=True, text=True,
                                    timeout=CLONE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return JSONResponse({"reason": "clone timed out"}, status_code=504)
            if r.returncode != 0:
                redacted = CREDENTIAL_RE.sub("://***@", r.stderr or "")
                access_log.error("clone failed for repo=%s: %s", repo, redacted)
                return JSONResponse({"reason": "clone failed"}, status_code=502)

        for e in goals.list_goals(path):
            if e.get("record_status") == "running" and e.get("planner_alive"):
                return JSONResponse({"reason": "goal already running", "goal_id": e["goal_id"]}, status_code=409)

        r = goals.start(path, goal, account_id=account, requester=requester)
        if not r.get("launched"):
            return JSONResponse({"reason": r.get("reason")}, status_code=422)
        return JSONResponse({"goal_id": r["goal_id"], "repo": repo, "status": "running", "requester": requester},
                             status_code=201)

    return _with_repo_lock(repo, run_under_lock)


def list_all_goals(request):
    auth_err = _auth_error(request)
    if auth_err:
        return auth_err

    repo_filter = request.query_params.get("repo")
    repos = _load_repos()

    if repo_filter:
        cfg, err = _resolve_repo(repo_filter)
        if err:
            return err
        slugs = [repo_filter]
    else:
        slugs = [s for s, cfg in repos.items() if "path" in cfg]

    out = []
    for slug in slugs:
        for e in goals.list_goals(repos[slug]["path"]):
            out.append({**e, "repo": slug})
    return JSONResponse({"goals": out}, status_code=200)


def get_goal(request):
    auth_err = _auth_error(request)
    if auth_err:
        return auth_err

    slug = request.path_params["repo"]
    cfg, err = _resolve_repo(slug)
    if err:
        return err

    entries = goals.status(cfg["path"], request.path_params["goal_id"])
    if not entries:
        return JSONResponse({"reason": "unknown goal"}, status_code=404)
    return JSONResponse({**entries[0], "repo": slug}, status_code=200)


def cancel_goal(request):
    auth_err = _auth_error(request)
    if auth_err:
        return auth_err

    slug = request.path_params["repo"]
    goal_id = request.path_params["goal_id"]
    cfg, err = _resolve_repo(slug)
    if err:
        return err

    r = goals.stop(cfg["path"], goal_id)
    if r.get("error") == "unknown goal":
        return JSONResponse({"reason": "unknown goal"}, status_code=404)
    return JSONResponse({"goal_id": goal_id, "repo": slug, "status": "stopped"}, status_code=200)


class AccessLogMiddleware:
    """Pure ASGI middleware (not a route handler, so the sync-handler rule doesn't apply): logs method, path,
    status only. The bearer token is a header value and is never read or logged here."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status_holder = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
        access_log.info("%s %s %s", scope["method"], scope["path"], status_holder.get("status"))


def create_app():
    app = Starlette(routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/goals", create_goal, methods=["POST"]),
        Route("/goals", list_all_goals, methods=["GET"]),
        Route("/goals/{repo}/{goal_id}", get_goal, methods=["GET"]),
        Route("/goals/{repo}/{goal_id}/cancel", cancel_goal, methods=["POST"]),
    ])
    app.add_middleware(AccessLogMiddleware)
    return app


def main(host="127.0.0.1", port=8090):
    if not os.environ.get("ORCH_SERVICE_TOKEN"):
        print("serve: ORCH_SERVICE_TOKEN must be set", file=sys.stderr)
        sys.exit(1)
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
