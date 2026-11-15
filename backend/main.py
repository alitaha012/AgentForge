"""AgentForge API — conversation in, running agent team out."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from fastapi import (Cookie, Depends, FastAPI, File, Form, HTTPException,
                     Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel

from . import (auth, builder, mirror, profiles, security, store, trust,
               voice, workspace)
from .adapters.hermes import build_runtime
from .adapters.orchestrator import build_orchestrator
from .architect import Architect
from .registry import REGISTRY
from .schema import TeamSpec, remove_agent, validate_spec

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

app = FastAPI(title="AgentForge", version="0.2")
# Credentials ride in a cookie, so a wildcard origin is not acceptable here:
# any page could then drive this API as the signed-in user.
@app.middleware("http")
async def _headers(request: Request, call_next):
    """Every response carries the headers; only HTML carries the policy.

    A JSON reply does not need a CSP, and sending one on every API call is
    noise in the logs for no gain.
    """
    response = await call_next(request)
    for k, v in security.HEADERS.items():
        response.headers.setdefault(k, v)
    if "text/html" in response.headers.get("content-type", ""):
        security.apply(response, Path(WEB_DIR) / "index.html")
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
        "AGENTFORGE_ALLOW_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

COOKIE = "agentforge_session"


def _runtime_hint(e: Exception) -> str:
    """Turn a runtime failure into something the user can act on."""
    msg = str(e).strip()
    low = msg.lower()
    if "source-update" in low or "hermes_bootstrap" in low or "resolving python" in low:
        return ("Hermes is part-way through a self-update and cannot answer yet. "
                "Run `hermes update` in a terminal, wait for it to finish, check "
                "`hermes -z \"ok\"` works, then try again.\n\n" + msg[:400])
    if "not found" in low and "hermes" in low:
        return ("The hermes command was not found. Check it is on PATH for the "
                "shell that started this app, or set HERMES_CLI_BIN.\n\n" + msg[:300])
    if "exceeded" in low and "timeout" not in low:
        return msg[:500]
    if "timed out" in low or "exceeded" in low:
        return ("Hermes did not answer in time. Raise HERMES_CLI_TIMEOUT, or check "
                "whether it is waiting on something in a terminal.\n\n" + msg[:300])
    if "budget" in low:
        return msg[:300]
    return "Hermes call failed: " + msg[:500]

RUNTIME = build_runtime()
ORCHESTRATOR = build_orchestrator(RUNTIME)
ARCHITECT = Architect(RUNTIME)


# ---------------------------------------------------------------- models
class ChatIn(BaseModel):
    session_id: str
    message: str


class RunIn(BaseModel):
    message: str


class ConfirmIn(BaseModel):
    spec: Optional[Dict[str, Any]] = None  # user-edited spec wins if provided


class AgentEditIn(BaseModel):
    nickname: Optional[str] = None
    personality: Optional[str] = None
    memory_enabled: Optional[bool] = None


class MemoryIn(BaseModel):
    content: str


class CredsIn(BaseModel):
    username: str
    password: str


class ChangePwIn(BaseModel):
    current_password: str
    new_password: str


# ---------------------------------------------------------------- auth deps
def current_user(agentforge_session: Optional[str] = Cookie(default=None)) -> dict:
    user = auth.user_for_token(agentforge_session or "")
    if not user:
        raise HTTPException(401, "Sign in first.")
    return user


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        COOKIE, token,
        max_age=auth.SESSION_TTL_DAYS * 86400,
        httponly=True,       # JavaScript cannot read it, so XSS cannot steal it
        samesite="lax",      # not sent on cross-site form posts
        secure=os.getenv("AGENTFORGE_COOKIE_SECURE", "0") == "1",
        path="/",
    )


def _client_ip(request: Request) -> str:
    """The caller's address.

    X-Forwarded-For is only believed when the deployment says it sits behind
    a proxy it trusts. Taking it on faith would let anyone set their own
    bucket by sending a header, which turns the per-address limit into
    decoration.
    """
    if os.getenv("AGENTFORGE_TRUST_PROXY", "0") == "1":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown")


def _login_buckets(username: str, request: Request) -> tuple:
    """Two independent limits: one on the account, one on the address.

    The account bucket stops a password being guessed from many machines;
    the address bucket stops one machine working through many accounts.
    Either tripping is enough.
    """
    return (f"user:{(username or '').strip().lower()}",
            f"ip:{_client_ip(request)}")


def _guard_login(username: str, request: Request) -> None:
    for bucket in _login_buckets(username, request):
        wait = store.login_locked_for(bucket)
        if wait > 0:
            secs = int(wait) + 1
            when = (f"{secs} seconds" if secs < 90
                    else f"{round(secs / 60)} minutes")
            # 429, and the same wording whichever bucket tripped: saying
            # "this account is locked" would confirm the account exists.
            raise HTTPException(
                429, f"Too many sign-in attempts. Try again in {when}.",
                headers={"Retry-After": str(secs)})


@app.post("/api/auth/signup")
def signup(body: CredsIn, response: Response, request: Request) -> Dict[str, Any]:
    _guard_login(body.username, request)
    uid, err = auth.sign_up(body.username, body.password)
    if err:
        # A rejected signup is also a probe for which names are taken.
        store.note_login_failure(f"ip:{_client_ip(request)}", "address")
        raise HTTPException(400, err)
    token, err = auth.log_in(
        body.username, body.password,
        agent=request.headers.get("user-agent", ""), ip=_client_ip(request))
    if err or not token:
        raise HTTPException(400, err or "Could not sign in.")
    _set_cookie(response, token)
    return {"username": body.username.strip()}


@app.post("/api/auth/login")
def login(body: CredsIn, response: Response, request: Request) -> Dict[str, Any]:
    _guard_login(body.username, request)
    token, err = auth.log_in(
        body.username, body.password,
        agent=request.headers.get("user-agent", ""), ip=_client_ip(request))
    if err or not token:
        for bucket, kind in zip(_login_buckets(body.username, request),
                                ("username", "address")):
            store.note_login_failure(bucket, kind)
        raise HTTPException(401, err or "Could not sign in.")
    # A correct password forgives what came before it.
    store.clear_login_failures(*_login_buckets(body.username, request))
    _set_cookie(response, token)
    return {"username": body.username.strip()}


@app.get("/api/auth/sessions")
def auth_sessions(user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Where this account is signed in. Never the tokens, only their shape."""
    return {"sessions": store.list_auth_sessions(user["id"])}


@app.post("/api/auth/sessions/revoke")
def auth_revoke(agentforge_session: Optional[str] = Cookie(default=None),
                user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Sign out everywhere except here.

    The session making the request is kept: signing the person out of the
    browser they are using to secure the account is not what they asked for.
    """
    gone = store.revoke_other_sessions(
        user["id"], auth.token_hash(agentforge_session or ""))
    return {"revoked": gone}


@app.post("/api/auth/logout")
def logout(response: Response,
           agentforge_session: Optional[str] = Cookie(default=None)) -> Dict[str, bool]:
    auth.log_out(agentforge_session or "")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(agentforge_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    user = auth.user_for_token(agentforge_session or "")
    return {
        "signed_in": bool(user),
        "username": user["username"] if user else None,
        "any_accounts": store.user_count() > 0,
    }


@app.post("/api/auth/password")
def change_password(body: ChangePwIn, response: Response,
                    user: dict = Depends(current_user)) -> Dict[str, bool]:
    err = auth.change_password(user["id"], body.current_password, body.new_password)
    if err:
        raise HTTPException(400, err)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- status
def current_admin(user: dict = Depends(current_user)) -> dict:
    if not store.is_admin(user["id"]):
        raise HTTPException(404, "Not found.")   # don't reveal the admin area
    return user


@app.post("/api/admin/users/{target}/toolsets")
def admin_set_toolsets(target: str, body: Dict[str, Any],
                       user: dict = Depends(current_admin)) -> Dict[str, Any]:
    """Widen or narrow what one account may grant its agents.

    Pass {"toolsets": [...]} to set it, or {"toolsets": null} to put the
    account back on the default policy.
    """
    names = body.get("toolsets", None)
    if names is not None and not isinstance(names, list):
        raise HTTPException(400, "toolsets must be a list, or null")
    store.set_allowed_toolsets(target, names)
    return {"user_id": target,
            "allowed": sorted(store.allowed_toolsets(target)
                              or trust.default_policy())}


@app.get("/api/admin/overview")
def admin_overview(user: dict = Depends(current_admin)) -> Dict[str, Any]:
    """Everything on this instance: who exists, what they built, what is running."""
    return {
        "stats": store.admin_stats(),
        "users": store.admin_users(),
        "teams": store.admin_teams(),
        "runs": store.admin_runs(60),
        "mirror": mirror.status(),
        "lockouts": store.login_lockouts(),
    }


@app.get("/api/admin/runs/{run_id}/trace")
def admin_trace(run_id: str, user: dict = Depends(current_admin)) -> Dict[str, Any]:
    return {"run_id": run_id, "trace": store.get_trace(run_id)}


@app.get("/api/diagnose")
def diagnose(user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Where does the time actually go?

    A trivial prompt costs only overhead — process start, config load, plugin
    init. Compare that with a real turn to see whether tuning prompts is worth
    anything, or whether the transport is the problem.
    """
    if not hasattr(RUNTIME, "probe"):
        raise HTTPException(400, "This runtime cannot be probed.")
    first = RUNTIME.probe()
    second = RUNTIME.probe()        # second run, caches warm
    health = RUNTIME.health()
    overhead = min(first["ms"], second["ms"])
    real = health.get("median_call_ms")
    return {
        "trivial_call_ms": {"first": first["ms"], "second": second["ms"]},
        "overhead_ms": overhead,
        "median_real_call_ms": real,
        "thinking_ms": (real - overhead) if (real and real > overhead) else None,
        "verdict": (
            "Most of the wait is startup, not the model. A persistent "
            "transport (hermes proxy) is the fix; prompt tuning will not help."
            if overhead and real and overhead > real * 0.5 else
            "Most of the wait is the model thinking. Lower the reasoning "
            "effort or use a smaller model for this role."
            if real else
            "Run a few turns first, then call this again for a comparison."),
    }


@app.get("/api/status")
def status(user: dict = Depends(current_user)) -> Dict[str, Any]:
    health = RUNTIME.health()
    orch: Dict[str, Any] = {"name": ORCHESTRATOR.name, "ok": True}
    return {
        "runtime": health,
        "orchestrator": orch,
        "tools": [
            {"name": n, "description": REGISTRY.get(n).description,
             "requires": REGISTRY.get(n).requires}
            for n in REGISTRY.tool_names()
        ],
        "missing_credentials": REGISTRY.missing_credentials(set(REGISTRY.tool_names())),
        "teams": store.list_teams(user["id"]),
        "username": user["username"],
        "is_admin": store.is_admin(user["id"]),
        "profiles": profiles.status(user["username"]),
    }


# ---------------------------------------------------------------- build flow
@app.post("/api/session")
def new_session(user: dict = Depends(current_user)) -> Dict[str, str]:
    return {"session_id": store.create_session(user["id"])}


def _own_session(sid: str, user: dict) -> None:
    owner = store.session_owner(sid)
    if owner != user["id"]:
        raise HTTPException(404, "No such session.")


@app.post("/api/chat")
def chat(body: ChatIn, user: dict = Depends(current_user)) -> Dict[str, Any]:
    _own_session(body.session_id, user)
    history = store.get_messages(body.session_id)
    owner = user["username"]
    try:
        staged = _describe(workspace.staging_dir(owner, body.session_id))
        text, spec, issues, history, progress = ARCHITECT.turn(
            history, body.message, owner, files=staged,
            allowed=(store.allowed_toolsets(user["id"])
                     or trust.default_policy()))
        if spec and any(i.level == "error" for i in issues):
            text, spec, issues, history, progress = ARCHITECT.repair(history, issues, owner)
    except Exception as e:
        # Hermes itself failed. Hand the real reason to the user instead of a
        # bare 500 whose cause only appears in the server log.
        raise HTTPException(503, _runtime_hint(e)) from e

    store.set_messages(body.session_id, history)

    payload: Dict[str, Any] = {
        "took_ms": getattr(RUNTIME, "last_ms", None),
        "reply": text,
        "issues": [i.model_dump() for i in issues],
        "spec": None,
        "ready": False,
        "progress": progress,
    }
    if spec:
        meta = store.save_spec(body.session_id, spec.model_dump(by_alias=True))
        payload["spec"] = spec.model_dump(by_alias=True)
        payload["spec_version"] = meta["version"]
        payload["ready"] = not any(i.level == "error" for i in issues)
    return payload


@app.post("/api/session/{session_id}/files")
async def session_upload(session_id: str,
                         files: list[UploadFile] = File(...),
                         paths: list[str] = Form(default=[]),
                         user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Accept files during the interview, before any team exists.

    They wait in a per-session folder and are moved into the team's workspace
    the moment it is built, so a person can describe the work and hand over
    the data in one go instead of being told to come back afterwards.
    """
    if store.session_owner(session_id) != user["id"]:
        raise HTTPException(404, "unknown session")
    root = workspace.staging_dir(user["username"], session_id)
    saved = []
    for i, f in enumerate(files):
        rel = paths[i] if i < len(paths) else ""
        saved.append(await _receive(f, root, rel))
    return {"ok": True, "saved": saved, "files": _describe(root)}


@app.get("/api/session/{session_id}/files")
def session_files(session_id: str,
                  user: dict = Depends(current_user)) -> Dict[str, Any]:
    if store.session_owner(session_id) != user["id"]:
        raise HTTPException(404, "unknown session")
    return {"files": _describe(workspace.staging_dir(user["username"],
                                                     session_id))}


async def _voice_to_text(clip: UploadFile, language: str) -> Dict[str, Any]:
    """Shared by both voice endpoints: write the clip, read it back as text.

    The recording goes to a temporary file rather than the workspace. A
    dictated sentence is a way of typing, and nobody expects every spoken
    message to leave an audio file behind for their agents to trip over.
    """
    if not voice.available():
        raise HTTPException(501, voice.how_to_install())

    import tempfile
    suffix = Path(clip.filename or "clip.webm").suffix or ".webm"
    tmp = Path(tempfile.gettempdir()) / f"af-voice-{os.urandom(6).hex()}{suffix}"
    try:
        tmp.write_bytes(await clip.read())
        if tmp.stat().st_size == 0:
            raise HTTPException(400, "the recording was empty")
        try:
            return voice.transcribe(str(tmp), language or None)
        except HTTPException:
            raise
        except Exception as e:
            # The usual first-run failure is the model download, which is the
            # one moment this feature needs the network. Say which it was.
            hint = ("The speech model could not be downloaded. The first "
                    "recording needs internet once; after that it is local."
                    if "connect" in str(e).lower() or "403" in str(e)
                    or "resolve" in str(e).lower() else str(e)[:300])
            raise HTTPException(503, hint) from e
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/voice")
def voice_status(user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Whether dictation will work, so the interface can say so up front."""
    return {"available": voice.available(), "model": voice.model_name(),
            "install": "" if voice.available() else voice.how_to_install()}


@app.post("/api/voice")
async def voice_transcribe(clip: UploadFile = File(...),
                           language: str = Form(default=""),
                           user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Turn a recording into text, on this machine."""
    return await _voice_to_text(clip, language)


@app.get("/api/spec/{session_id}")
def get_spec(session_id: str, user: dict = Depends(current_user)) -> Dict[str, Any]:
    _own_session(session_id, user)
    latest = store.latest_spec(session_id)
    if not latest:
        raise HTTPException(404, "no spec drafted in this session yet")
    return latest


@app.post("/api/spec/{session_id}/confirm")
def confirm(session_id: str, body: ConfirmIn,
            user: dict = Depends(current_user)) -> Dict[str, Any]:
    _own_session(session_id, user)
    raw = body.spec or (store.latest_spec(session_id) or {}).get("spec")
    if not raw:
        raise HTTPException(404, "no spec to confirm")
    try:
        spec = TeamSpec.model_validate(raw)
    except Exception as e:
        raise HTTPException(422, f"spec invalid: {e}")

    issues = validate_spec(spec, REGISTRY)
    if any(i.level == "error" for i in issues):
        return {"created": False, "issues": [i.model_dump() for i in issues]}

    owner = user["username"]
    # What this account may grant, checked before anything is written.
    denied = trust.policy_violations(spec, store.allowed_toolsets(user["id"]))
    if denied:
        raise HTTPException(403, " ".join(denied))

    built = builder.build(spec, owner)                     # role 2: agents to disk
    runtime_ref = ORCHESTRATOR.create_team(spec, owner)    # register with runtime
    team_id = store.save_team(session_id, spec.model_dump(by_alias=True),
                              ORCHESTRATOR.name, runtime_ref, user["id"])
    mirror.team_created(owner, spec.model_dump(by_alias=True), str(built["team_dir"]))
    # Anything dropped into the chat during the interview belongs to this
    # team now, so it is waiting in the workspace on the very first run.
    adopted = workspace.adopt_staged(owner, session_id, spec.team_name)
    return {
        "created": True,
        "team_id": team_id,
        "runtime": ORCHESTRATOR.name,
        "runtime_ref": runtime_ref,
        "built": built,
        "adopted_files": adopted,
        "issues": [i.model_dump() for i in issues],
    }


# ---------------------------------------------------------------- run flow
@app.get("/api/teams")
def teams(user: dict = Depends(current_user)) -> Dict[str, Any]:
    return {"teams": store.list_teams(user["id"])}


@app.get("/api/teams/{team_id}")
def team_detail(team_id: str, user: dict = Depends(current_user)) -> Dict[str, Any]:
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "unknown team")
    return team


@app.delete("/api/teams/{team_id}")
def delete_team(team_id: str, user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Delete a team: its agents, their memories, its workspace and its runs.

    The folder goes first and the database row second. If the folder cannot
    be removed, nothing is deleted from the database either, so the team is
    still listed and the person can try again — the opposite order would
    leave files on disk that nothing in the app can reach or clean up.
    """
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "No such team.")

    removed = builder.remove_team_dir(team["name"], user["username"])
    if not removed and builder.team_dir(team["name"], user["username"]):
        from pathlib import Path as _P
        if _P(builder.team_dir(team["name"], user["username"])).is_dir():
            raise HTTPException(
                500, "The team's folder could not be removed, so nothing was "
                     "deleted. A file in it may be open in another program.")

    counts = store.delete_team(team_id, user["id"]) or {}
    return {"deleted": team["name"], "folder_removed": removed, **counts}


def _team_or_404(team_id: str, user: dict) -> dict:
    team = store.get_team(team_id, user["id"])
    if not team:
        # Same answer whether the team is missing or belongs to someone else:
        # a different message would confirm that another user's team exists.
        raise HTTPException(404, "unknown team")
    return team


@app.get("/api/teams/{team_id}/workspace")
def workspace_list(team_id: str,
                   user: dict = Depends(current_user)) -> Dict[str, Any]:
    """What this team can see. Scoped to the caller: no other user's files."""
    team = _team_or_404(team_id, user)
    root = workspace.ensure(user["username"], team["name"])
    return {"files": _describe(root), "note": workspace.advice(),
            "usage": workspace.usage(root)}


def _describe(root: Path) -> list:
    """Workspace contents as the interface shows them: path, size and kind."""
    out = []
    for n in workspace.listing(root):
        try:
            size = (root / n).stat().st_size
        except OSError:
            continue
        out.append({"name": n, "bytes": size, "kind": workspace.kind_of(n)})
    return out


async def _receive(upload: UploadFile, root: Path, rel: str) -> Dict[str, Any]:
    """Write one upload into a workspace, wherever the browser said it sat.

    `rel` is the browser's own relative path, which for a folder drop carries
    the directory structure the person is expecting to keep. It is
    attacker-controlled, so it is rebuilt segment by segment rather than
    trusted, and the final path is checked to be inside the workspace before
    a single byte is written.
    """
    raw = rel or (upload.filename or "upload")
    safe = workspace.safe_relpath(raw) or "upload"
    target = root / safe
    if not workspace.inside(root, target):
        raise HTTPException(400, f"bad path: {raw!r}")

    limit = int(os.getenv("AGENTFORGE_UPLOAD_MAX_MB", "25")) * 1024 * 1024
    size, chunks = 0, []
    while chunk := await upload.read(1 << 20):
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                413, f"{safe} is over the {limit // (1024*1024)}MB limit")
        chunks.append(chunk)

    # Checked once the size is known, so the reason names the real number.
    full = workspace.room_for(root, size)
    if full:
        raise HTTPException(413, full)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"".join(chunks))
    # Scanned on arrival, not on use: the person who uploaded it is here now
    # and can say whether that line is meant to be there.
    findings = trust.scan_file(target, name=safe)
    return {"name": safe, "bytes": size, "kind": workspace.kind_of(safe),
            "warnings": findings}


# FastAPI needs python-multipart to parse an upload, and it raises while the
# route is being *declared*, not when it is called. Without this guard a venv
# that predates the dependency cannot start the server at all: the whole app
# dies at import over one endpoint. Registering a stub instead keeps every
# other route working and turns a traceback into a sentence.
try:                                  # the package renamed itself; accept both
    import python_multipart           # noqa: F401
    _MULTIPART = True
except ImportError:                   # pragma: no cover
    try:
        import multipart              # noqa: F401
        _MULTIPART = True
    except ImportError:
        _MULTIPART = False

_NO_MULTIPART = ("File upload needs the python-multipart package. "
                 "Install it with: pip install python-multipart "
                 "(or re-run run.ps1, which installs requirements.txt).")


async def workspace_upload(team_id: str,
                           files: list[UploadFile] = File(...),
                           paths: list[str] = Form(default=[]),
                           user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Put files where the team can reach them, folders included.

    This is the only way in. Agents cannot open the user's disk, so anything
    they need is copied here first. `paths` carries each file's position
    inside a dropped folder; it is sanitised, never trusted.
    """
    team = _team_or_404(team_id, user)
    root = workspace.ensure(user["username"], team["name"])
    saved = []
    for i, f in enumerate(files):
        rel = paths[i] if i < len(paths) else ""
        saved.append(await _receive(f, root, rel))
    return {"ok": True, "saved": saved, "files": _describe(root)}


def workspace_upload_unavailable(team_id: str) -> Dict[str, Any]:
    raise HTTPException(501, _NO_MULTIPART)


# Registration, not definition, is what inspects the signature and raises, so
# the route carrying UploadFile is only ever handed to FastAPI when the parser
# behind it exists.
app.post("/api/teams/{team_id}/workspace")(
    workspace_upload if _MULTIPART else workspace_upload_unavailable)


@app.post("/api/teams/{team_id}/workspace/adopt")
def workspace_adopt(team_id: str, session_id: str = "",
                    user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Move files left in an interview's staging area into an existing team.

    Files dropped while describing the work are held per session and handed
    to the team that session creates. If no team is ever created from it, the
    upload is stranded: it is on disk, it belongs to nobody, and nothing in
    the interface mentions it again. This lets the person put it where they
    meant it to go instead of uploading it a second time.
    """
    team = _team_or_404(team_id, user)
    owner = user["username"]
    if session_id and store.session_owner(session_id) != user["id"]:
        raise HTTPException(404, "unknown session")

    moved: list = []
    roots = ([session_id] if session_id
             else [d.name for d in workspace.all_staging(owner)])
    for sid in roots:
        moved += workspace.adopt_staged(owner, sid, team["name"])
    return {"moved": moved,
            "files": _describe(workspace.ensure(owner, team["name"]))}


@app.get("/api/staging")
def staging_summary(user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Anything uploaded during an interview that never reached a team."""
    owner = user["username"]
    out = []
    for d in workspace.all_staging(owner):
        files = _describe(d)
        if files:
            out.append({"session_id": d.name, "files": files})
    return {"stranded": out,
            "count": sum(len(s["files"]) for s in out)}


@app.delete("/api/teams/{team_id}/workspace/{name:path}")
def workspace_delete(team_id: str, name: str,
                     user: dict = Depends(current_user)) -> Dict[str, Any]:
    team = _team_or_404(team_id, user)
    root = workspace.ensure(user["username"], team["name"])
    try:
        target = workspace.resolve(root, name)
    except ValueError:
        raise HTTPException(400, "bad filename")
    if target.is_file():
        target.unlink()
    return {"ok": True}


@app.patch("/api/teams/{team_id}/agents/{agent_name}")
def edit_agent(team_id: str, agent_name: str, body: AgentEditIn,
               user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Rename, re-voice or change the memory setting of one agent.

    Rewrites that agent's AGENTS.md so Hermes picks the change up on its next
    run. The agent's own memory file is never touched.
    """
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "No such team.")
    spec = TeamSpec.model_validate(team["spec"])

    target = None
    if spec.main_agent.name == agent_name:
        target = spec.main_agent
    else:
        target = next((a for a in spec.sub_agents if a.name == agent_name), None)
    if target is None:
        raise HTTPException(404, "No such agent on this team.")

    for field in ("nickname", "personality", "memory_enabled"):
        value = getattr(body, field)
        if value is not None:
            setattr(target, field, value)

    issues = validate_spec(spec, REGISTRY)
    if any(i.level == "error" for i in issues):
        raise HTTPException(422, "; ".join(i.message for i in issues
                                           if i.level == "error"))

    built = builder.build(spec, user["username"])     # leaves memory.md alone
    store.update_team_spec(team_id, spec.model_dump(by_alias=True))
    return {"saved": True, "agent": target.model_dump(by_alias=True),
            "built": built, "issues": [i.model_dump() for i in issues]}


@app.delete("/api/teams/{team_id}/agents/{agent_name}")
def delete_agent(team_id: str, agent_name: str,
                 user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Remove one sub-agent from a team, for good.

    The chart is repaired first (reports promoted, dangling `manages` entries
    and handoff rules dropped) and the result is validated before anything is
    written. If the team would not be valid without this agent, nothing
    changes and the reason is returned — better to refuse than to leave a
    team that cannot run.
    """
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "No such team.")
    spec = TeamSpec.model_validate(team["spec"])

    if spec.main_agent.name == agent_name:
        raise HTTPException(
            400, "The supervisor cannot be deleted; it is the team. Delete "
                 "the team instead, or build a new one.")
    if not any(a.name == agent_name for a in spec.sub_agents):
        raise HTTPException(404, "No such agent on this team.")
    if len(spec.sub_agents) <= 1:
        raise HTTPException(
            400, "This is the team's last agent. A supervisor with nobody to "
                 "delegate to cannot run.")

    notes = remove_agent(spec, agent_name)

    issues = validate_spec(spec, REGISTRY)
    errors = [i.message for i in issues if i.level == "error"]
    if errors:
        raise HTTPException(422, "Cannot remove that agent: " + "; ".join(errors))

    built = builder.build(spec, user["username"])
    removed = builder.remove_agent_dir(team["name"], agent_name,
                                       user["username"])
    store.update_team_spec(team_id, spec.model_dump(by_alias=True))
    return {"deleted": agent_name, "folder_removed": removed,
            "changes": notes, "built": built,
            "agents": [a.name for a in spec.sub_agents],
            "issues": [i.model_dump() for i in issues]}


@app.get("/api/teams/{team_id}/agents/{agent_name}/memory")
def read_agent_memory(team_id: str, agent_name: str,
                      user: dict = Depends(current_user)) -> Dict[str, Any]:
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "No such team.")
    path = Path(builder.agent_dir(team["name"], agent_name, user["username"])) \
        / builder.MEMORY_FILE
    return {"exists": path.is_file(),
            "content": path.read_text(encoding="utf-8") if path.is_file() else "",
            "path": str(path)}


@app.put("/api/teams/{team_id}/agents/{agent_name}/memory")
def write_agent_memory(team_id: str, agent_name: str, body: MemoryIn,
                       user: dict = Depends(current_user)) -> Dict[str, Any]:
    """The memory is the user's to prune as much as the agent's to write."""
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "No such team.")
    d = Path(builder.agent_dir(team["name"], agent_name, user["username"]))
    if not d.is_dir():
        raise HTTPException(404, "No such agent on this team.")
    (d / builder.MEMORY_FILE).write_text(body.content[:40000], encoding="utf-8")
    return {"saved": True}


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, user: dict = Depends(current_user)) -> StreamingResponse:
    """Retry a run, keeping whatever already succeeded.

    A run that died on one agent's timeout threw away every result before it.
    The finished steps are copied into the new run, so the agents that already
    answered are not asked again — only the work that never completed is.
    """
    old = store.get_run(run_id, user["id"])
    if not old:
        raise HTTPException(404, "unknown run")
    team = store.get_team(old["team_id"], user["id"])
    if not team:
        raise HTTPException(404, "unknown team")
    spec = TeamSpec.model_validate(team["spec"])
    new_id = store.start_run(old["team_id"], old["message"], user["id"])
    reused = store.copy_steps(run_id, new_id)
    return _stream_run(spec, old["message"], user, new_id,
                       note=f"resumed from {run_id}: {reused} finished "
                            f"step(s) carried over")


@app.post("/api/teams/{team_id}/run")
def run_team(team_id: str, body: RunIn,
             user: dict = Depends(current_user)) -> StreamingResponse:
    team = store.get_team(team_id, user["id"])
    if not team:
        raise HTTPException(404, "unknown team")
    spec = TeamSpec.model_validate(team["spec"])
    run_id = store.start_run(team_id, body.message, user["id"])
    return _stream_run(spec, body.message, user, run_id)


def _stream_run(spec: TeamSpec, message: str, user: dict, run_id: str,
                note: str = "") -> StreamingResponse:

    def gen():
        if hasattr(RUNTIME, "begin_run"):
            RUNTIME.begin_run()
        yield _sse({"kind": "run_started", "run_id": run_id})
        if note:
            yield _sse({"kind": "resumed", "agent": "system", "text": note})
        answer = ""
        try:
            for ev in ORCHESTRATOR.run(spec, message,
                                       owner=user["username"],
                                       run_id=run_id):
                store.add_trace(run_id, ev.get("agent", ""), ev.get("kind", ""),
                                ev.get("text", "")[:4000])
                if ev.get("kind") == "agent_result":
                    store.bump_run_agents(run_id)
                if ev.get("kind") == "final":
                    answer = ev.get("text", "")
                yield _sse(ev)
        except Exception as e:
            failed = _runtime_hint(e)
            store.add_trace(run_id, "system", "error", failed)
            yield _sse({"kind": "error", "agent": "system", "text": failed})
        finally:
            store.finish_run(run_id, answer)
            mirror.run_finished(user["username"], team["name"], run_id,
                                body.message, answer, store.get_trace(run_id))
            yield _sse({"kind": "run_finished", "run_id": run_id})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/trace")
def trace(run_id: str, user: dict = Depends(current_user)) -> Dict[str, Any]:
    if store.run_owner(run_id) != user["id"]:
        raise HTTPException(404, "No such run.")
    return {"run_id": run_id, "trace": store.get_trace(run_id)}


def _sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- web page
if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(WEB_DIR, "index.html"))
