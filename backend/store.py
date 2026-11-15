"""SQLite persistence: build sessions, versioned specs, teams, runs, traces."""
from __future__ import annotations

import hashlib
import threading
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from .envs import int_env

DB_PATH = os.getenv("AGENTFORGE_DB", os.path.join(os.path.dirname(__file__), "agentforge.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created REAL, messages TEXT
);
CREATE TABLE IF NOT EXISTS specs (
  id TEXT PRIMARY KEY, session_id TEXT, version INTEGER, spec TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY, session_id TEXT, name TEXT, spec TEXT,
  runtime TEXT, runtime_ref TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, team_id TEXT, message TEXT, answer TEXT,
  created REAL, finished REAL
);
CREATE TABLE IF NOT EXISTS trace (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL,
  agent TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT UNIQUE, pwd TEXT,
  created REAL, last_login REAL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT, created REAL, expires REAL,
  last_used REAL, agent TEXT, ip TEXT
);
CREATE INDEX IF NOT EXISTS ix_auth_user ON auth_sessions(user_id);
-- One row per piece of work a team finished. A run that dies part-way can be
-- resumed from these instead of paying for the same agent calls twice. The
-- key is a hash of (agent, instruction), so a retry that asks the same agent
-- for the same thing is recognised, and a retry that asks something new is
-- not.
CREATE TABLE IF NOT EXISTS run_steps (
  run_id TEXT, step_key TEXT, agent TEXT, instruction TEXT,
  result TEXT, created REAL,
  PRIMARY KEY (run_id, step_key)
);
CREATE INDEX IF NOT EXISTS ix_steps_run ON run_steps(run_id);
-- Failed sign-in attempts, kept per bucket (a username, or an address).
-- On disk rather than in memory so a restart is not a way to clear a
-- lockout, and so the admin can see that one happened at all.
CREATE TABLE IF NOT EXISTS login_fails (
  bucket TEXT PRIMARY KEY, kind TEXT, fails INTEGER,
  first_at REAL, last_at REAL, locked_until REAL
);
"""

# Added after the first release: widen existing tables in place.
_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN user_id TEXT",
    "ALTER TABLE teams ADD COLUMN user_id TEXT",
    "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN user_id TEXT",
    "ALTER TABLE runs ADD COLUMN status TEXT",
    "ALTER TABLE runs ADD COLUMN agent_calls INTEGER DEFAULT 0",
]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


# Workers run concurrently, so several threads commit on this one connection.
# sqlite3 is built serialized, but two interleaved write+commit pairs can still
# raise under contention; this makes each write atomic from Python's side.
_LOCK = threading.RLock()

_C = _conn()
_C.executescript(_SCHEMA)
for _stmt in _MIGRATIONS:
    try:
        _C.execute(_stmt)
    except sqlite3.OperationalError:
        pass  # column already present
_C.commit()


def nid() -> str:
    return uuid.uuid4().hex[:12]


# ---- sessions -------------------------------------------------------------
def create_session(user_id: str) -> str:
    sid = nid()
    _C.execute("INSERT INTO sessions (id,created,messages,user_id) VALUES (?,?,?,?)",
               (sid, time.time(), "[]", user_id))
    _C.commit()
    return sid


def session_owner(sid: str) -> Optional[str]:
    row = _C.execute("SELECT user_id FROM sessions WHERE id=?", (sid,)).fetchone()
    return row["user_id"] if row else None


def get_messages(sid: str) -> List[Dict[str, str]]:
    row = _C.execute("SELECT messages FROM sessions WHERE id=?", (sid,)).fetchone()
    return json.loads(row["messages"]) if row else []


def set_messages(sid: str, messages: List[Dict[str, str]]) -> None:
    _C.execute("UPDATE sessions SET messages=? WHERE id=?",
               (json.dumps(messages, ensure_ascii=False), sid))
    _C.commit()


# ---- specs ----------------------------------------------------------------
def save_spec(sid: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    row = _C.execute("SELECT MAX(version) v FROM specs WHERE session_id=?", (sid,)).fetchone()
    version = (row["v"] or 0) + 1
    spec_id = nid()
    _C.execute("INSERT INTO specs VALUES (?,?,?,?,?)",
               (spec_id, sid, version, json.dumps(spec, ensure_ascii=False), time.time()))
    _C.commit()
    return {"spec_id": spec_id, "version": version}


def latest_spec(sid: str) -> Optional[Dict[str, Any]]:
    row = _C.execute(
        "SELECT * FROM specs WHERE session_id=? ORDER BY version DESC LIMIT 1", (sid,)
    ).fetchone()
    if not row:
        return None
    return {"spec_id": row["id"], "version": row["version"], "spec": json.loads(row["spec"])}


# ---- teams ----------------------------------------------------------------
def save_team(sid: str, spec: Dict[str, Any], runtime: str, runtime_ref: str,
              user_id: str) -> str:
    tid = nid()
    _C.execute(
        "INSERT INTO teams (id,session_id,name,spec,runtime,runtime_ref,created,user_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (tid, sid, spec.get("team_name", "team"),
         json.dumps(spec, ensure_ascii=False), runtime, runtime_ref, time.time(),
         user_id))
    _C.commit()
    return tid


def update_team_spec(tid: str, spec: Dict[str, Any]) -> None:
    _C.execute("UPDATE teams SET spec=? WHERE id=?",
               (json.dumps(spec, ensure_ascii=False), tid))
    _C.commit()


def get_team(tid: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """With user_id given, a team belonging to anyone else is simply not found."""
    if user_id is None:
        row = _C.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
    else:
        row = _C.execute("SELECT * FROM teams WHERE id=? AND user_id=?",
                         (tid, user_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["spec"] = json.loads(d["spec"])
    return d


def delete_team(tid: str, user_id: Optional[str] = None) -> Optional[Dict[str, int]]:
    """Delete a team and everything that belongs only to it.

    Runs, their traces and their checkpointed steps go with it. Keeping them
    would leave rows pointing at a team that no longer exists: the admin view
    joins runs to teams, so orphans would be counted but not attributable,
    and nobody could open them. The caller is told how much went, because
    deleting a team quietly also deleting its history would be a surprise.

    Returns None when the team is not this user's, so a caller can answer 404
    without revealing that someone else's team has that id.
    """
    if get_team(tid, user_id) is None:
        return None
    with _LOCK:
        runs = [r["id"] for r in
                _C.execute("SELECT id FROM runs WHERE team_id=?", (tid,))]
        traces = steps = 0
        for rid in runs:
            traces += _C.execute("DELETE FROM trace WHERE run_id=?",
                                 (rid,)).rowcount or 0
            steps += _C.execute("DELETE FROM run_steps WHERE run_id=?",
                                (rid,)).rowcount or 0
        _C.execute("DELETE FROM runs WHERE team_id=?", (tid,))
        _C.execute("DELETE FROM teams WHERE id=?", (tid,))
        _C.commit()
    return {"runs": len(runs), "trace_rows": traces, "steps": steps}


def list_teams(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if user_id is None:
        rows = _C.execute(
            "SELECT id,name,runtime,created FROM teams ORDER BY created DESC"
        ).fetchall()
    else:
        rows = _C.execute(
            "SELECT id,name,runtime,created FROM teams WHERE user_id=?"
            " ORDER BY created DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


# ---- runs & trace ---------------------------------------------------------
def start_run(team_id: str, message: str, user_id: str = "") -> str:
    rid = nid()
    _C.execute("INSERT INTO runs (id,team_id,message,answer,created,finished,"
               "user_id,status,agent_calls) VALUES (?,?,?,?,?,?,?,?,?)",
               (rid, team_id, message, None, time.time(), None, user_id,
                "running", 0))
    _C.commit()
    return rid


def finish_run(run_id: str, answer: str, status: str = "") -> None:
    if not status:
        status = "done" if answer else "failed"
    _C.execute("UPDATE runs SET answer=?, finished=?, status=? WHERE id=?",
               (answer, time.time(), status, run_id))
    _C.commit()


def bump_run_agents(run_id: str) -> None:
    with _LOCK:
        _C.execute(
            "UPDATE runs SET agent_calls = COALESCE(agent_calls,0)+1 WHERE id=?",
            (run_id,))
        _C.commit()


# Five tries before the first pause, then the wait doubles: 1, 2, 4, 8
# minutes, capped. Slow enough to make guessing pointless, forgiving enough
# that a person who mistypes twice notices nothing.
LOGIN_FREE_TRIES = int_env("AGENTFORGE_LOGIN_FREE_TRIES", 5)
LOGIN_BASE_LOCK_S = int_env("AGENTFORGE_LOGIN_LOCK_S", 60)
LOGIN_MAX_LOCK_S = int_env("AGENTFORGE_LOGIN_MAX_LOCK_S", 900)
LOGIN_WINDOW_S = int_env("AGENTFORGE_LOGIN_WINDOW_S", 3600)


def login_locked_for(bucket: str) -> float:
    """Seconds still to wait on this bucket, or 0."""
    row = _C.execute("SELECT locked_until FROM login_fails WHERE bucket=?",
                     (bucket,)).fetchone()
    if not row or not row["locked_until"]:
        return 0.0
    return max(0.0, float(row["locked_until"]) - time.time())


def note_login_failure(bucket: str, kind: str) -> float:
    """Record one failure. Returns the lock it earned, in seconds."""
    now = time.time()
    with _LOCK:
        row = _C.execute("SELECT * FROM login_fails WHERE bucket=?",
                         (bucket,)).fetchone()
        # A long-quiet bucket starts over: yesterday's typo should not count
        # toward today's lockout.
        if row and now - float(row["last_at"] or 0) > LOGIN_WINDOW_S:
            row = None
        fails = (row["fails"] if row else 0) + 1
        over = max(0, fails - LOGIN_FREE_TRIES)
        lock = 0.0
        if over:
            lock = min(LOGIN_BASE_LOCK_S * (2 ** (over - 1)), LOGIN_MAX_LOCK_S)
        _C.execute(
            "INSERT OR REPLACE INTO login_fails "
            "(bucket,kind,fails,first_at,last_at,locked_until) "
            "VALUES (?,?,?,?,?,?)",
            (bucket, kind, fails,
             (row["first_at"] if row else now), now, now + lock))
        _C.commit()
    return lock


def clear_login_failures(*buckets: str) -> None:
    """A correct password forgives everything that came before it."""
    with _LOCK:
        for b in buckets:
            _C.execute("DELETE FROM login_fails WHERE bucket=?", (b,))
        _C.commit()


def login_lockouts() -> List[Dict[str, Any]]:
    """Buckets currently locked, for the admin view."""
    now = time.time()
    rows = _C.execute(
        "SELECT * FROM login_fails WHERE locked_until > ? ORDER BY last_at DESC",
        (now,)).fetchall()
    return [{"bucket": r["bucket"], "kind": r["kind"], "fails": r["fails"],
             "seconds_left": int(float(r["locked_until"]) - now)} for r in rows]


def step_key(agent: str, instruction: str) -> str:
    """Identity of a unit of work: who was asked, and what for."""
    return hashlib.sha256(
        f"{agent}\x00{(instruction or '').strip()}".encode("utf-8")
    ).hexdigest()[:16]


def save_step(run_id: str, agent: str, instruction: str, result: str) -> None:
    with _LOCK:
        _C.execute(
            "INSERT OR REPLACE INTO run_steps "
            "(run_id,step_key,agent,instruction,result,created) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, step_key(agent, instruction), agent, instruction, result,
             time.time()))
        _C.commit()


def get_step(run_id: str, agent: str, instruction: str) -> Optional[str]:
    if not run_id:
        return None
    row = _C.execute(
        "SELECT result FROM run_steps WHERE run_id=? AND step_key=?",
        (run_id, step_key(agent, instruction))).fetchone()
    return row["result"] if row else None


def copy_steps(from_run: str, to_run: str) -> int:
    """Carry a failed run's finished work into its retry."""
    rows = _C.execute("SELECT step_key,agent,instruction,result FROM run_steps "
                      "WHERE run_id=?", (from_run,)).fetchall()
    for r in rows:
        _C.execute("INSERT OR REPLACE INTO run_steps "
                   "(run_id,step_key,agent,instruction,result,created) "
                   "VALUES (?,?,?,?,?,?)",
                   (to_run, r["step_key"], r["agent"], r["instruction"],
                    r["result"], time.time()))
    _C.commit()
    return len(rows)


def add_trace(run_id: str, agent: str, kind: str, detail: str) -> None:
    with _LOCK:
        _C.execute(
            "INSERT INTO trace (run_id,ts,agent,kind,detail) VALUES (?,?,?,?,?)",
            (run_id, time.time(), agent, kind, detail))
        _C.commit()


def get_trace(run_id: str) -> List[Dict[str, Any]]:
    rows = _C.execute("SELECT ts,agent,kind,detail FROM trace WHERE run_id=? ORDER BY id",
                      (run_id,)).fetchall()
    return [dict(r) for r in rows]


# ---- accounts & auth sessions --------------------------------------------
def create_user(username: str, pwd_hash: str) -> str:
    uid = nid()
    # The first account to exist is the admin; later ones are ordinary users.
    first = _C.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
    _C.execute("INSERT INTO users (id,username,pwd,created,last_login,is_admin)"
               " VALUES (?,?,?,?,?,?)",
               (uid, username, pwd_hash, time.time(), None, 1 if first else 0))
    _C.commit()
    return uid


def find_user(username: str) -> Optional[Dict[str, Any]]:
    row = _C.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    row = _C.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def touch_user(user_id: str) -> None:
    _C.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))
    _C.commit()


def set_user_password(user_id: str, pwd_hash: str) -> None:
    _C.execute("UPDATE users SET pwd=? WHERE id=?", (pwd_hash, user_id))
    _C.commit()


def user_count() -> int:
    return _C.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def _ensure_user_columns() -> None:
    have = {r["name"] for r in _C.execute("PRAGMA table_info(users)")}
    if "allowed_toolsets" not in have:
        _C.execute("ALTER TABLE users ADD COLUMN allowed_toolsets TEXT")
        _C.commit()


def set_allowed_toolsets(user_id: str, names) -> None:
    """Which toolsets this account may grant. None means the default policy."""
    with _LOCK:
        _C.execute("UPDATE users SET allowed_toolsets=? WHERE id=?",
                   (json.dumps(sorted(names)) if names is not None else None,
                    user_id))
        _C.commit()


def allowed_toolsets(user_id: str):
    row = _C.execute("SELECT allowed_toolsets FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if not row or not row["allowed_toolsets"]:
        return None
    try:
        return set(json.loads(row["allowed_toolsets"]))
    except (TypeError, ValueError):
        return None


def _ensure_session_columns() -> None:
    """Add the newer columns to a database made before they existed.

    Cheaper and safer than a migration file for three nullable columns, and
    it means an existing install keeps its sessions instead of signing
    everyone out on upgrade.
    """
    have = {r["name"] for r in _C.execute("PRAGMA table_info(auth_sessions)")}
    for col in ("last_used REAL", "agent TEXT", "ip TEXT"):
        if col.split()[0] not in have:
            _C.execute(f"ALTER TABLE auth_sessions ADD COLUMN {col}")
    _C.commit()


_ensure_session_columns()      # run once, now that the helpers exist
_ensure_user_columns()


def create_auth_session(token_hash: str, user_id: str, expires: float,
                        agent: str = "", ip: str = "") -> None:
    # Columns named explicitly: an older database that gained its newer
    # columns by ALTER has them in a different order than a fresh one.
    _C.execute("INSERT OR REPLACE INTO auth_sessions "
               "(token_hash,user_id,created,expires,last_used,agent,ip) "
               "VALUES (?,?,?,?,?,?,?)",
               (token_hash, user_id, time.time(), expires,
                time.time(), (agent or "")[:200], ip or ""))
    _C.execute("DELETE FROM auth_sessions WHERE expires < ?", (time.time(),))
    _C.commit()


def get_auth_session(token_hash: str) -> Optional[Dict[str, Any]]:
    row = _C.execute("SELECT * FROM auth_sessions WHERE token_hash=?",
                     (token_hash,)).fetchone()
    return dict(row) if row else None


def delete_auth_session(token_hash: str) -> None:
    _C.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
    _C.commit()


def delete_user_auth_sessions(user_id: str) -> None:
    _C.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
    _C.commit()


def get_run(run_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One run. With user_id, another person's run is simply not found."""
    sql = "SELECT * FROM runs WHERE id=?"
    args: tuple = (run_id,)
    if user_id is not None:
        sql += " AND user_id=?"
        args += (user_id,)
    row = _C.execute(sql, args).fetchone()
    return dict(row) if row else None


def touch_auth_session(token_hash: str) -> None:
    """Record that a session was used, so a stale one is recognisable."""
    with _LOCK:
        _C.execute("UPDATE auth_sessions SET last_used=? WHERE token_hash=?",
                   (time.time(), token_hash))
        _C.commit()


def list_auth_sessions(user_id: str) -> List[Dict[str, Any]]:
    """A user's live sessions. Never the token, only its shape and age."""
    rows = _C.execute(
        "SELECT token_hash,created,last_used,agent,ip,expires FROM auth_sessions "
        "WHERE user_id=? AND expires > ? ORDER BY last_used DESC",
        (user_id, time.time())).fetchall()
    return [{"id": r["token_hash"][:12], "created": r["created"],
             "last_used": r["last_used"], "agent": r["agent"] or "",
             "ip": r["ip"] or "", "expires": r["expires"]} for r in rows]


def revoke_other_sessions(user_id: str, keep_hash: str) -> int:
    """Sign out everywhere else, keeping the session asking for it."""
    with _LOCK:
        cur = _C.execute(
            "DELETE FROM auth_sessions WHERE user_id=? AND token_hash<>?",
            (user_id, keep_hash))
        _C.commit()
    return cur.rowcount or 0


def run_owner(run_id: str) -> Optional[str]:
    row = _C.execute(
        "SELECT t.user_id u FROM runs r JOIN teams t ON t.id = r.team_id"
        " WHERE r.id = ?", (run_id,)).fetchone()
    return row["u"] if row else None


# ---- admin views ---------------------------------------------------------
def is_admin(user_id: str) -> bool:
    row = _C.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def admin_users() -> List[Dict[str, Any]]:
    rows = _C.execute(
        "SELECT u.id, u.username, u.created, u.last_login, u.is_admin,"
        " (SELECT COUNT(*) FROM teams t WHERE t.user_id = u.id) teams,"
        " (SELECT COUNT(*) FROM runs r WHERE r.user_id = u.id) runs"
        " FROM users u ORDER BY u.created").fetchall()
    return [dict(r) for r in rows]


def admin_teams() -> List[Dict[str, Any]]:
    """Every team on the instance, with its owner and its agent roster."""
    rows = _C.execute(
        "SELECT t.id, t.name, t.created, t.runtime, t.runtime_ref, t.spec,"
        " u.username owner,"
        " (SELECT COUNT(*) FROM runs r WHERE r.team_id = t.id) runs,"
        " (SELECT MAX(r.created) FROM runs r WHERE r.team_id = t.id) last_run"
        " FROM teams t LEFT JOIN users u ON u.id = t.user_id"
        " ORDER BY t.created DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        spec = json.loads(d.pop("spec") or "{}")
        d["supervisor"] = (spec.get("main_agent") or {}).get("name", "supervisor")
        d["agents"] = [
            {"name": a.get("name"), "role": a.get("role"),
             "toolsets": a.get("toolsets") or [], "skills": a.get("skills") or [],
             "reasoning": a.get("reasoning")}
            for a in spec.get("sub_agents") or []
        ]
        out.append(d)
    return out


def admin_runs(limit: int = 50) -> List[Dict[str, Any]]:
    rows = _C.execute(
        "SELECT r.id, r.message, r.status, r.created, r.finished, r.agent_calls,"
        " t.name team, u.username owner,"
        " (SELECT COUNT(*) FROM trace x WHERE x.run_id = r.id) events"
        " FROM runs r LEFT JOIN teams t ON t.id = r.team_id"
        " LEFT JOIN users u ON u.id = r.user_id"
        " ORDER BY r.created DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def admin_stats() -> Dict[str, Any]:
    q = lambda s, *a: _C.execute(s, a).fetchone()[0]
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "teams": q("SELECT COUNT(*) FROM teams"),
        "runs": q("SELECT COUNT(*) FROM runs"),
        "running_now": q("SELECT COUNT(*) FROM runs WHERE status='running'"),
        "runs_24h": q("SELECT COUNT(*) FROM runs WHERE created > ?",
                      time.time() - 86400),
    }
