"""One folder per team, and that folder is the whole world.

Hermes's local terminal backend runs on the host with no isolation: its file
tools can read or delete anything the user running the server can. Nothing in
`--in` changes that — that flag only says where AGENTS.md lives. So the
boundary has to be drawn here, before a path ever reaches the agent.

Three rules, enforced in three different places so that defeating one is not
enough:

  1. Nothing outside the workspace is ever *named* to an agent. User text is
     scrubbed of absolute paths before it becomes a task (`scrub`), and the
     app never hands out a host path of its own.
  2. The agent's process starts *inside* the workspace (`cwd`), so every
     relative path it invents resolves there and nowhere else.
  3. Whatever an agent says is checked on the way back (`escapes`), and a run
     that names a path outside its workspace is flagged to the owner rather
     than passed along quietly.

This is a strong fence, not a kernel. An agent that deliberately writes an
absolute path into a tool call can still reach the disk, because the Hermes
local backend permits it. The only real jail on this platform is the Docker
terminal backend; `advice()` says so rather than letting the fence be
mistaken for one.

Isolation between users and between teams falls out of the layout: a
workspace path is derived from the owner and the team name, both of which are
sanitised, and `resolve()` refuses anything that lands outside its own root.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple

from .builder import TEAMS_DIR, _safe

WORKSPACE = "workspace"

# What an escape attempt looks like in text. These are deliberately broad:
# a false positive costs one scrubbed string, a false negative costs the disk.
_PATTERNS = [
    # A drive letter, then path segments that may themselves contain spaces
    # ("ZEINA TAHA"). A space is only consumed when what follows it is another
    # separated segment, so "C:\temp and then" keeps the "and then".
    (re.compile(r"\b[A-Za-z]:[\\/]\S*(?:[ ]\S*[\\/]\S*)*"), "a Windows path"),
    (re.compile(r"\\\\[^\s\"'<>|]+"), "a network share"),
    (re.compile(
        r"(?<![\w.])/(?:etc|root|home|usr|var|proc|sys|bin|opt|mnt|srv)"
        r"(?:/[^\s\"'<>|]*)?"), "a system path"),
    (re.compile(r"%[A-Za-z_]+%"), "a Windows environment variable"),
    (re.compile(r"\$(?:HOME|USERPROFILE|APPDATA|PATH)\b"), "an environment variable"),
    (re.compile(r"(?<![\w])~[\\/]\S*"), "a home-directory path"),
    # Every consecutive step, not just the first: "../../etc/passwd" is one
    # escape, and removing one `../` from it leaves a working path behind.
    (re.compile(r"(?<![\w.])(?:\.\.[\\/])+[^\s\"'<>|]*"),
     "a parent-directory step"),
]


def root_for(owner: str, team: str) -> Path:
    """The one directory a team may touch."""
    return TEAMS_DIR / _safe(owner) / _safe(team) / WORKSPACE


def ensure(owner: str, team: str) -> Path:
    d = root_for(owner, team)
    d.mkdir(parents=True, exist_ok=True)
    return d


def inside(root: Path, candidate: Path) -> bool:
    """True only if candidate resolves within root.

    Compared on resolved paths, so a symlink pointing out, a `..` segment and
    a drive change all fail. `Path.is_relative_to` is used rather than string
    prefixing, which would accept `/teams/ali-evil` for root `/teams/ali`.
    """
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def resolve(root: Path, relative: str) -> Path:
    """Turn an agent-supplied relative path into a real one, or refuse.

    Judged by inspecting the string, not only by where it lands, so the answer
    is the same on every platform. A backslash is a separator on Windows and
    an ordinary character on Linux: `..\\..\\x` escapes on one and does not on
    the other, and a rule that holds in testing but not in production is worse
    than no rule. Both are refused here.

    Absolute paths are refused rather than reinterpreted: silently turning
    `/etc/passwd` into `<workspace>/etc/passwd` would make a caller believe it
    reached something it did not.
    """
    raw = (relative or "").strip()
    if not raw:
        raise ValueError("empty path")
    if raw[0] in "/\\" or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"absolute paths are not allowed: {relative!r}")
    if "\\" in raw:
        raise ValueError(f"use '/' between folders, not '\\': {relative!r}")
    if any(part == ".." for part in raw.split("/")):
        raise ValueError(f"path leaves the workspace: {relative!r}")

    target = (root / raw).resolve()
    if not inside(root, target):            # symlinks, and anything missed
        raise ValueError(f"path leaves the workspace: {relative!r}")
    return target


def escapes(text: str) -> List[str]:
    """Every out-of-workspace reference in a piece of text, described."""
    found: List[str] = []
    for pattern, what in _PATTERNS:
        for m in pattern.finditer(text or ""):
            found.append(f"{what}: {m.group(0).strip()[:80]}")
    return found


def scrub(text: str) -> Tuple[str, List[str]]:
    """Strip host paths out of text before an agent ever reads it.

    Returns the cleaned text and what was removed, so the app can tell the
    user plainly instead of silently changing what they asked for.
    """
    found = escapes(text)
    cleaned = text or ""
    for pattern, _ in _PATTERNS:
        cleaned = pattern.sub("[path removed - use a file in the workspace]",
                              cleaned)
    return cleaned, found


STAGING = "_staging"

# What a file is, for the person and for the architect. The point is not
# precision about formats: it is whether an agent needs the `vision` toolset
# to make any use of it.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif",
                  ".tiff", ".svg", ".heic"}
TEXTY_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml",
                  ".xml", ".html", ".log", ".ini", ".cfg", ".py", ".js",
                  ".sql", ".sh", ".ps1"}


def kind_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in TEXTY_SUFFIXES:
        return "text"
    return "other"


def safe_relpath(raw: str) -> str:
    """Turn a browser-supplied path into one that cannot leave the workspace.

    A folder upload sends `webkitRelativePath`, which is attacker-controlled
    in exactly the same way a filename is. Every segment is sanitised with
    the same rule used for team and agent names, `..` and empty segments are
    dropped outright, and the depth is capped so a deeply nested drop cannot
    be used to exhaust the filesystem. The result is always relative.
    """
    parts: List[str] = []
    for seg in re.split(r"[\\/]+", (raw or "").strip()):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", seg).strip(". ")
        if cleaned:
            parts.append(cleaned[:64])
    if not parts:
        return ""
    # Cap the folders, never the file: truncating the whole list would turn
    # `a/b/.../x.csv` into a file called by its deepest folder name, which
    # loses the upload under a name the person never used.
    *folders, name = parts
    return "/".join(folders[:7] + [name])


def staging_dir(owner: str, session_id: str) -> Path:
    """Where files dropped during the interview wait for a team to exist."""
    from .builder import TEAMS_DIR, _safe
    d = TEAMS_DIR / _safe(owner) / STAGING / _safe(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def all_staging(owner: str) -> List[Path]:
    """Every staging folder this owner has, newest first.

    Empty ones are left for the caller to filter: knowing a session existed
    but held nothing is sometimes the answer.
    """
    from .builder import TEAMS_DIR, _safe
    root = TEAMS_DIR / _safe(owner) / STAGING
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)


def adopt_staged(owner: str, session_id: str, team: str) -> List[str]:
    """Move interview uploads into the team's workspace once it is built.

    Copy-then-remove rather than rename: the two can sit on different drives,
    and a failed move that loses the person's upload is much worse than a
    duplicate left behind.
    """
    import shutil
    src = staging_dir(owner, session_id)
    dest = ensure(owner, team)
    moved: List[str] = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        moved.append(rel.as_posix())
    if moved:
        shutil.rmtree(src, ignore_errors=True)
    return moved


# A team's folder is the only place it can write, which also makes it the
# only place it can fill. Agents write through Hermes, not through this app,
# so the ceiling cannot be enforced on their writes — it is checked on the
# way in, and reported after a run so a loop is visible rather than silent.
MAX_MB = int(os.getenv("AGENTFORGE_WORKSPACE_MAX_MB", "200"))
MAX_FILES = int(os.getenv("AGENTFORGE_WORKSPACE_MAX_FILES", "500"))


def usage(root: Path) -> Dict[str, Any]:
    """Bytes and file count in a workspace, with the ceilings."""
    total = files = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return {"bytes": total, "files": files,
            "max_bytes": MAX_MB * 1024 * 1024, "max_files": MAX_FILES,
            "over": total > MAX_MB * 1024 * 1024 or files > MAX_FILES}


def room_for(root: Path, incoming: int) -> str:
    """Empty string if the file fits, otherwise why it does not."""
    u = usage(root)
    if u["files"] + 1 > MAX_FILES:
        return (f"This workspace already holds {u['files']} files, which is "
                f"the limit of {MAX_FILES}. Remove some first.")
    if u["bytes"] + incoming > u["max_bytes"]:
        used = u["bytes"] / 1048576
        return (f"This workspace holds {used:.1f}MB of its {MAX_MB}MB limit, "
                f"so that file does not fit. Remove some first.")
    return ""


def listing(root: Path, limit: int = 200) -> List[str]:
    """Workspace contents as relative paths: what an agent may be told about."""
    if not root.is_dir():
        return []
    out: List[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and inside(root, p):
            out.append(p.relative_to(root).as_posix())
            if len(out) >= limit:
                break
    return out


def rule_text() -> str:
    """The paragraph every agent's AGENTS.md carries."""
    return (
        "## Your workspace\n\n"
        "You work inside one folder. It is your current directory, and it is "
        "the only place you may read or write.\n\n"
        "- Refer to files by their name alone: `sales.csv`, not a full path.\n"
        "- Never use a drive letter, a leading `/`, `~`, `..`, or an "
        "environment variable in a path.\n"
        "- If you are asked for something outside this folder, say you cannot "
        "reach it and ask for the file to be put in the workspace. Do not "
        "guess at a location on the machine.\n"
        "- Files you create stay here. Another team cannot see them, and "
        "neither can another person's agents.\n"
    )


def advice() -> str:
    """What this module does not do, stated where someone will read it."""
    return (
        "Agents are confined by this app, not by the operating system. "
        "Hermes's local terminal backend can still reach the disk if an agent "
        "writes an absolute path into a tool call. For an enforced boundary, "
        "run Hermes with the Docker terminal backend "
        "(hermes config set terminal.backend docker)."
    )
