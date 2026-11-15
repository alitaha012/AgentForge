"""Closed tool registry. The Architect may only pick from here, which is what
keeps generated teams actually runnable."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Set


@dataclass
class Tool:
    name: str
    description: str
    requires: List[str] = field(default_factory=list)  # env var names
    run: Callable[[str], str] | None = None


def _web_search(query: str) -> str:
    # Prefer a local SearXNG instance so the whole stack can stay on-machine.
    searx = os.getenv("SEARX_BASE_URL")
    if searx:
        url = searx.rstrip("/") + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json"}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=20) as r:
            data = json.loads(r.read())
        out = [f"- {o.get('title')}: {(o.get('content') or '')[:200]} ({o.get('url')})"
               for o in data.get("results", [])[:5]]
        return "\n".join(out) or "no results"

    key = os.getenv("SERPER_API_KEY")
    if not key:
        return ("[web_search unavailable: set SEARX_BASE_URL for a local search "
                "instance, or SERPER_API_KEY for the cloud one]")
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=json.dumps({"q": query}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    out = [f"- {o.get('title')}: {o.get('snippet')} ({o.get('link')})"
           for o in data.get("organic", [])[:5]]
    return "\n".join(out) or "no results"


def _fetch_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "[fetch_url: need an http(s) url]"
    req = urllib.request.Request(url, headers={"User-Agent": "agentforge/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(200_000).decode("utf-8", "replace")
    import re
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body)[:8000]


def _calculator(expr: str) -> str:
    import ast
    import operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
           ast.Pow: op.pow, ast.USub: op.neg, ast.Mod: op.mod}

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp):
            return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp):
            return ops[type(n.op)](ev(n.operand))
        raise ValueError("unsupported expression")
    try:
        return str(ev(ast.parse(expr, mode="eval").body))
    except Exception as e:
        return f"[calculator error: {e}]"


def _notes_store() -> Dict[str, str]:
    return _NOTES


_NOTES: Dict[str, str] = {}


def _save_note(arg: str) -> str:
    key, _, val = arg.partition("=")
    _NOTES[key.strip()] = val.strip()
    return f"saved note '{key.strip()}'"


def _read_notes(_: str) -> str:
    return json.dumps(_NOTES, ensure_ascii=False) if _NOTES else "no notes yet"


class ToolRegistry:
    def __init__(self) -> None:
        self._cat: Dict[str, list] | None = None
        self._tools: Dict[str, Tool] = {}
        for t in [
            Tool("web_search", "Search the web for a query, returns top result snippets.",
                 ["SERPER_API_KEY"], _web_search),
            Tool("fetch_url", "Fetch a web page and return its text content.", [], _fetch_url),
            Tool("calculator", "Evaluate an arithmetic expression.", [], _calculator),
            Tool("save_note", "Persist a key=value note for other agents in the team.", [],
                 _save_note),
            Tool("read_notes", "Read all notes saved by the team.", [], _read_notes),
        ]:
            self._tools[t.name] = t

    def tool_names(self) -> List[str]:
        return list(self._tools)

    # --- Hermes-native toolsets and skills -----------------------------------
    # What `-t` and `-s` accept, read from hermes_catalogue.json (generated from
    # this machine's config.yaml platform_toolsets and skills directory).
    # HERMES_TOOLSETS / HERMES_SKILLS override it as comma-separated names.
    def _catalogue(self) -> Dict[str, list]:
        if self._cat is None:
            path = Path(os.getenv(
                "HERMES_CATALOGUE",
                Path(__file__).resolve().parent.parent / "hermes_catalogue.json",
            ))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._cat = {"toolsets": data.get("toolsets") or [],
                             "skills": data.get("skills") or []}
            except Exception:
                self._cat = {"toolsets": [], "skills": []}
        return self._cat

    def toolset_names(self) -> List[str]:
        raw = os.getenv("HERMES_TOOLSETS", "")
        if raw.strip():
            return [t.strip() for t in raw.split(",") if t.strip()]
        return [t["name"] for t in self._catalogue()["toolsets"]]

    def skill_names(self) -> List[str]:
        raw = os.getenv("HERMES_SKILLS", "")
        if raw.strip():
            return [s.strip() for s in raw.split(",") if s.strip()]
        return [s["name"] for s in self._catalogue()["skills"]]

    def hermes_catalogue_for_prompt(self, full: bool = True) -> str:
        """full=False lists names only.

        While the Architect is still asking questions it does not need every
        skill's description — that is ~500 tokens on every turn for information
        it only uses once, when it designs the team.
        """
        if not full:
            ts = ", ".join(self.toolset_names())
            sk = ", ".join(self.skill_names())
            return (f"TOOLSETS: {ts}\n\nSKILLS: {sk}\n\n"
                    "(Descriptions are supplied when you come to design the team.)")
        cat = self._catalogue()
        allowed_ts = set(self.toolset_names())
        allowed_sk = set(self.skill_names())
        lines = ['TOOLSETS (grant with the agent\'s "toolsets" field):']
        for t in cat["toolsets"]:
            if t["name"] in allowed_ts:
                lines.append(f"- {t['name']}: {t.get('desc','')}")
        for n in sorted(allowed_ts - {t["name"] for t in cat["toolsets"]}):
            lines.append(f"- {n}")
        skills = [s for s in cat["skills"] if s["name"] in allowed_sk]
        if skills or allowed_sk:
            lines.append('\nSKILLS (grant with the agent\'s "skills" field):')
            for s in skills:
                lines.append(f"- {s['name']}: {s.get('desc','')}")
            for n in sorted(allowed_sk - {s["name"] for s in cat["skills"]}):
                lines.append(f"- {n}")
        return "\n".join(lines)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def missing_credentials(self, tool_names: Set[str]) -> List[str]:
        missing: List[str] = []
        for n in tool_names:
            t = self._tools.get(n)
            if not t:
                continue
            for env in t.requires:
                if not os.getenv(env) and env not in missing:
                    missing.append(env)
        return missing

    def catalogue_for_prompt(self) -> str:
        lines = []
        for t in self._tools.values():
            creds = f" (needs {', '.join(t.requires)})" if t.requires else ""
            lines.append(f"- {t.name}: {t.description}{creds}")
        return "\n".join(lines)

    def call(self, name: str, arg: str) -> str:
        t = self._tools.get(name)
        if not t or not t.run:
            return f"[tool '{name}' not available]"
        try:
            return t.run(arg)
        except Exception as e:  # tools must never crash a run
            return f"[tool '{name}' failed: {e}]"


REGISTRY = ToolRegistry()
