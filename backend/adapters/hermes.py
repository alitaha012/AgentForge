"""Hermes runtime adapters. Everything here is local — no cloud API keys.

Three transports, selected with HERMES_TRANSPORT:

  cli   (default) - shell out to the local `hermes` binary, which routes inference
                    through your Antigravity subscription via its model-provider
                    plugin (agy as model only; Hermes/our registry own the tools).
  http            - POST to a local OpenAI-compatible endpoint, if your Hermes
                    build or gateway exposes one.
  mock            - fully offline stand-in, spends no quota.

All three satisfy the same `chat()` signature, so nothing else in the app changes.
"""
from __future__ import annotations

import json
import os
import re
import time
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Protocol

from ..envs import int_env

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# Reasoning models emit their deliberation before the answer. Qwen3 wraps it in
# <think>…</think>; Ollama's CLI prints "Thinking... … ...done thinking.".
# Left in, it becomes the reply — and an agent's DELEGATE:/FINAL: line would be
# buried in it.
_THINK_TAG = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning)>.*\Z", re.S | re.I)
_THINK_CLI = re.compile(r"^\s*Thinking\.\.\..*?\.\.\.done thinking\.\s*",
                        re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Remove a model's visible deliberation, keeping only its answer."""
    text = _THINK_TAG.sub("", text)
    text = _THINK_CLI.sub("", text)
    # an unclosed block means the answer never arrived; keep what came before
    text = _THINK_OPEN.sub("", text)
    return text.strip()
# Hermes TUI chrome: box drawing, braille banner art, rule lines, status footer.
_BOX = re.compile(r"^[\s─-╿⠀-⣿█░▭●]*$")
_REPLY_MARKER = re.compile(r"^[\s─-╿]*(?:☤|✦)?\s*(?:Hermes|claude[\w.\-]*)\s*$",
                           re.I)


# Variables a child process genuinely needs to function. Everything else is
# withheld: the server holds model keys so that it can route calls, and an
# agent that inherits them can read them out of its own environment without
# touching a single file. On Windows SystemRoot and PATHEXT are not optional
# — without them sockets fail and the launcher cannot even be found.
_ENV_KEEP = (
    "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "WINDIR",
    "TEMP", "TMP", "TMPDIR",
    "HOME", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "PYTHONIOENCODING", "SHELL", "USER", "USERNAME", "LOGNAME",
)


def agent_env() -> Dict[str, str]:
    """The environment an agent's process gets: what it needs, nothing more.

    Hermes keeps its own provider credentials in its config directory and
    reads them from disk, so withholding the server's variables does not
    stop it signing in. If a provider genuinely needs one passed through,
    name it in AGENTFORGE_AGENT_ENV_KEEP rather than handing over the lot —
    and prefer putting it in the Hermes profile, where the agent cannot read
    it back as plain text.
    """
    keep = set(_ENV_KEEP)
    extra = os.getenv("AGENTFORGE_AGENT_ENV_KEEP", "")
    keep |= {k.strip().upper() for k in extra.split(",") if k.strip()}

    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    # Arabic in a team name or a file goes through this pipe; without this
    # a child that writes to stdout can die on an encode error.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def redact_argv(argv: List[str]) -> str:
    """The command we ran, with the prompt replaced by its size.

    A failure like `invalid_params` says nothing about which flag Hermes
    objected to, and the prompt is thousands of characters, so printing the
    raw command buries the one useful line. This keeps every flag and drops
    only the payload.
    """
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in ("-z", "--oneshot") and i + 1 < len(argv):
            out += [a, f"<prompt {len(argv[i + 1])} chars>"]
            skip = True
        else:
            out.append(a)
    return " ".join(out)


def extract_reply(raw: str) -> str:
    """Pull the model's answer out of Hermes CLI output, TUI chrome included.

    The interactive renderer wraps the reply between a '☤ Hermes' marker line and
    a horizontal rule, after an ASCII banner and a tool/skill panel. A truly
    non-interactive invocation prints the bare reply, which falls through intact.
    """
    text = strip_reasoning(_ANSI.sub("", raw).replace("\r", ""))
    lines = text.split("\n")

    # Prefer the last '☤ Hermes' block if the TUI rendered one.
    starts = [i for i, l in enumerate(lines) if _REPLY_MARKER.match(l.strip())]
    if starts:
        i = starts[-1] + 1
        block: List[str] = []
        while i < len(lines):
            s = lines[i].strip()
            if s and _BOX.match(s) and len(s) >= 3:  # closing rule
                break
            block.append(lines[i])
            i += 1
        candidate = "\n".join(block).strip()
        if candidate:
            return candidate

    # Otherwise drop panel/banner/footer lines and keep the prose.
    kept = [
        l for l in lines
        if l.strip()
        and not _BOX.match(l.strip())
        and not l.lstrip().startswith(("│", "╭", "╰", "┏", "┡", "└", "┃", "❯", "✦"))
        and "Welcome to Hermes Agent" not in l
        and not re.match(r"^\s*\d+\s+tools\b", l)
    ]
    out = "\n".join(kept).strip()
    return re.sub(r"^(ASSISTANT|Assistant)\s*:\s*", "", out).strip()


def flatten(messages: List[Dict[str, str]]) -> str:
    """Collapse a role transcript into one prompt for one-shot CLI invocation."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    body = "\n\n".join(
        f"{'USER' if m['role'] == 'user' else 'ASSISTANT'}: {m['content']}" for m in turns
    )
    parts = []
    if system:
        parts.append("<instructions>\n" + system + "\n</instructions>")
    parts.append("<conversation>\n" + body + "\n</conversation>")
    parts.append("Reply as ASSISTANT for the final USER turn. "
                 "Output only the reply itself, with no role label and no commentary.")
    return "\n\n".join(parts)


class Runtime(Protocol):
    name: str

    supports_native_tools: bool

    def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.3,
             max_tokens: int = 1200, **kwargs) -> str: ...

    def health(self) -> Dict[str, object]: ...


class HermesCliRuntime:
    """Drives the local `hermes` binary one query at a time.

    IMPORTANT: `hermes chat -q` is NOT one-shot on 0.23.x — it opens the
    interactive TUI, which hangs a subprocess call. HERMES_CLI_ARGS must name a
    genuinely non-interactive invocation, e.g.
        HERMES_CLI_ARGS=-z {prompt}
    {prompt} is substituted; everything else is passed through verbatim.

    Whatever the flag, `extract_reply` below strips the TUI banner and chrome, so
    a build that still prints them does not poison the model's answer.
    """
    name = "hermes-cli"

    def __init__(self) -> None:
        self.bin = os.getenv("HERMES_CLI_BIN", "hermes")
        # Empty by default: Hermes uses model.provider / model.default from its
        # own config.yaml, which is the invocation verified to work on this
        # machine. Set these only to override.
        self.provider = os.getenv("HERMES_PROVIDER", "").strip()
        self.model = os.getenv("HERMES_MODEL", "").strip()
        # Per-role overrides. The interview is one conversation that decides the
        # whole team, so it is worth a capable model; the agents then run many
        # calls each and are the expensive part, so they can run somewhere
        # cheaper (a local Ollama/LM Studio model, say) without hurting design
        # quality. Falls back to the values above, then to Hermes' own config.
        # The interview is a pure conversation: it calls no tools, needs no
        # skills, and reads no files. Loading all of Hermes' toolsets and skills
        # into its system prompt costs startup time and tokens on every turn,
        # which is why short exchanges felt slow. Restrict it.
        #   HERMES_INTERVIEW_TOOLSETS=""      -> no toolsets at all
        #   HERMES_INTERVIEW_LEAN=0           -> keep AGENTS.md/memory injection
        self.interview_toolsets = os.getenv("HERMES_INTERVIEW_TOOLSETS", "clarify").strip()
        self.interview_lean = os.getenv("HERMES_INTERVIEW_LEAN", "1") == "1"
        self.interview_model = os.getenv("HERMES_INTERVIEW_MODEL", "").strip()
        self.interview_provider = os.getenv("HERMES_INTERVIEW_PROVIDER", "").strip()
        self.worker_model = os.getenv("HERMES_WORKER_MODEL", "").strip()
        self.worker_provider = os.getenv("HERMES_WORKER_PROVIDER", "").strip()
        # Default generous: a research agent with skills loaded and several
        # web fetches routinely needs minutes, and a timeout kills the whole
        # delegation step.
        self.timeout = int_env("HERMES_CLI_TIMEOUT", 900)
        self.args_template = os.getenv("HERMES_CLI_ARGS", "-z {prompt}").strip()
        # Profiles isolate Hermes instances. The interview belongs on the profile
        # that carries the user's real memory (Honcho); worker runs belong on a
        # profile without it, so machine-written delegation text never becomes
        # part of the user's representation.
        self.profile = os.getenv("HERMES_PROFILE", "").strip()
        self.worker_profile = os.getenv("HERMES_WORKER_PROFILE", "").strip()

    # In one-shot mode Hermes runs its own tools, so callers do not need a
    # tool-call protocol of their own.
    supports_native_tools = True

    def _for_role(self, role: str, model: str, provider: str):
        """An explicit per-agent value wins, then the role default, then the
        global default, then Hermes' own config."""
        if role == "interview":
            return (model or self.interview_model or self.model,
                    provider or self.interview_provider or self.provider)
        return (model or self.worker_model or self.model,
                provider or self.worker_provider or self.provider)

    def _argv(self, prompt: str, *, toolsets=None, skills=None,
              reasoning: str = "", session: str = "", workdir: str = "",
              role: str = "interview", profile: str = "",
              model: str = "", provider: str = "") -> List[str]:
        # An explicit profile (per-user, from profiles.for_role) wins over the
        # static env defaults.
        if not profile:
            profile = self.worker_profile if role == "worker" else self.profile
        argv = [self.bin]
        if profile:
            argv += ["-p", profile]       # must precede the rest
        argv += [
            prompt if a == "{prompt}" else a.replace("{prompt}", prompt)
            for a in self.args_template.split()
        ]
        use_model, use_provider = self._for_role(role, model, provider)
        if use_provider:
            argv += ["--provider", use_provider]
        if use_model:
            argv += ["--model", use_model]
        if role == "interview" and not toolsets:
            # An empty value still means "these and no others" to Hermes.
            argv += ["--toolsets", self.interview_toolsets]
            if self.interview_lean:
                # Skips AGENTS.md, SOUL.md and preloaded-skill injection. The
                # Architect's instructions come from this app, not from disk.
                argv.append("--ignore-rules")
        elif toolsets:
            argv += ["--toolsets", ",".join(toolsets)]
        if skills:
            argv += ["--skills", ",".join(skills)]
        if reasoning:
            argv += ["--reasoning", reasoning]
        if session:
            argv += ["--resume", session]
        if workdir:
            # Hermes loads AGENTS.md from this directory, which is how a built
            # agent gets its identity.
            argv += ["--in", workdir]
        return argv

    def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.3,
             max_tokens: int = 1200, toolsets=None, skills=None,
             reasoning: str = "", session: str = "", workdir: str = "",
             role: str = "interview", profile: str = "", timeout_s: int = 0,
             model: str = "", provider: str = "", stage: str = "",
             cwd: str = "") -> str:
        argv = self._argv(flatten(messages), toolsets=toolsets, skills=skills,
                          reasoning=reasoning, session=session, workdir=workdir,
                          role=role, profile=profile, model=model,
                          provider=provider)
        limit = timeout_s or self.timeout
        try:
            # encoding must be explicit: on Windows, text=True decodes with the
            # ANSI code page (cp1252), which mangles em-dashes and smart quotes
            # and destroys any non-Latin script, Arabic included.
            # cwd is the team's workspace. Without it Hermes inherits the
            # server's directory, so a relative path an agent invents lands in
            # this app's source tree. With it, every relative path resolves
            # inside the one folder the team is allowed to touch.
            p = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=limit,
                               env=agent_env(),
                               cwd=(cwd if cwd and os.path.isdir(cwd) else None))
        except FileNotFoundError:
            raise RuntimeError(f"hermes binary not found: {self.bin}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"hermes call exceeded {limit}s. If this agent does web research "
                f"or many tool calls, raise its timeout_s, or HERMES_CLI_TIMEOUT.")
        if p.returncode != 0:
            raise RuntimeError(
                f"hermes exited {p.returncode}: "
                f"{(p.stderr or p.stdout).strip()[:500]}"
                f"  ·  command: {redact_argv(argv)}"
            )
        return extract_reply(p.stdout)

    def _label(self, role: str) -> str:
        m, pr = self._for_role(role, "", "")
        return f"{role}: {(pr + '/' + m).strip('/') if (m or pr) else 'config default'}"

    def health(self) -> Dict[str, object]:
        path = shutil.which(self.bin)
        # (profiles are reported in the status payload for visibility)
        if not path:
            return {"ok": False, "runtime": "hermes-cli",
                    "error": f"'{self.bin}' not on PATH"}
        try:
            p = subprocess.run([self.bin, "--version"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=20)
            ver = _ANSI.sub("", (p.stdout or p.stderr)).strip().splitlines()[:1]
            return {"ok": p.returncode == 0, "runtime": "hermes-cli", "base_url": path,
                    "models": [self._label("interview"), self._label("worker")],
                    "version": ver[0] if ver else "",
                    "profile": self.profile or "(default)",
                    "worker_profile": self.worker_profile or "(same as default)"}
        except Exception as e:
            return {"ok": False, "runtime": "hermes-cli", "error": str(e)}


class HermesRuntime:
    name = "hermes"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None) -> None:
        raw = (base_url or os.getenv("HERMES_BASE_URL", "http://127.0.0.1:1234")).rstrip("/")
        # Accept both forms: paths are appended as /v1/..., so a base that
        # already ends in /v1 would otherwise become /v1/v1/chat/completions.
        self.base_url = raw[:-3].rstrip("/") if raw.endswith("/v1") else raw
        self.model = model or os.getenv("HERMES_MODEL", "hermes")
        # `hermes proxy` accepts any bearer token and attaches the real
        # credentials itself, so a placeholder is correct here.
        self.api_key = api_key or os.getenv("HERMES_API_KEY", "") or "local"

    def _post(self, path: str, payload: dict, timeout: int = 120) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode(), headers=headers
        )
        # Local endpoint: must not go through any outbound proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())

    supports_native_tools = False

    def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.3,
             max_tokens: int = 1200, reasoning: str = "", model: str = "",
             **_ignored) -> str:
        data = self._post("/v1/chat/completions", {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        })
        msg = data["choices"][0]["message"]
        # some servers put deliberation in its own field, some inline it
        return strip_reasoning(msg.get("content") or "")

    def health(self) -> Dict[str, object]:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.base_url + "/v1/models", timeout=5) as r:
                models = json.loads(r.read())
            ids = [m.get("id") for m in models.get("data", [])]
            return {"ok": True, "runtime": "hermes", "base_url": self.base_url, "models": ids}
        except Exception as e:
            return {"ok": False, "runtime": "hermes", "base_url": self.base_url, "error": str(e)}


class MockHermes:
    """Deterministic stand-in: enough behaviour to exercise the full UI flow."""
    name = "mock"

    def __init__(self) -> None:
        self.turn = 0

    supports_native_tools = False

    def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.3,
             max_tokens: int = 1200, **_ignored) -> str:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_turns = [m for m in messages if m["role"] == "user"]
        last = user_turns[-1]["content"] if user_turns else ""

        # Sub-agent call: use one allowed tool, then answer.
        if "You are a sub-agent" in system:
            already_used_tool = any("Tool result:" in m["content"] for m in messages
                                    if m["role"] == "user")
            tools = re.findall(r"^- (\w+):", system, re.M)
            if tools and not already_used_tool:
                return f"TOOL: {tools[0]}(2+2)"
            return f"[mock sub-agent] result for: {last[:160]}"

        # Supervisor call: delegate once to each teammate, then finalise.
        if "Your team:" in system:
            roster = re.findall(r"^- (\w+):", system, re.M)
            done = [m["content"] for m in messages
                    if m["role"] == "user" and m["content"].startswith("Result from ")]
            delegated = {re.match(r"Result from (\w+)", d).group(1) for d in done}
            pending = [a for a in roster if a not in delegated]
            if pending:
                return f"DELEGATE: {pending[0]} | handle this part: {last[:120]}"
            return (f"FINAL: [mock supervisor] merged results from "
                    f"{', '.join(roster)} for the user's request.")

        # Architect: emulate the real 7-slot interview, including the
        # <progress> block, so the mock exercises the same UI path as Hermes.
        if "You are the Architect" in system:
            slots = ["outcome", "deliverable", "steps", "sources", "trigger",
                     "limits", "success"]
            answers = [m["content"] for m in user_turns]
            filled = {s: answers[i][:60] for i, s in enumerate(slots)
                      if i < len(answers)}
            missing = slots[len(answers):]
            confirmed = any(w in (answers[-1].lower() if answers else "")
                            for w in ("confirm", "build it", "go ahead", "yes"))
            progress = {"filled": filled, "missing": missing, "ready": not missing}

            if missing:
                body = (f"Thanks. Next I need the {missing[0]}:\n"
                        f"1. Can you describe the {missing[0]} you have in mind?")
            elif not confirmed:
                body = ("Here is the team I would build:\n"
                        "- supervisor — coordinates and merges the results\n"
                        "- researcher — gathers the material\n"
                        "- writer — produces the deliverable\n\n"
                        "Confirm and I will create it, or tell me what to change.")
            else:
                spec = {
                    "team_name": "requested_team",
                    "summary": f"Team for: {answers[0][:80]}",
                    "main_agent": {
                        "name": "supervisor",
                        "goal": answers[0][:200],
                        "reasoning": "high",
                        "system_prompt": "You coordinate the team and merge the "
                                         "sub-agents' results into one answer.",
                    },
                    "sub_agents": [
                        {"name": "researcher", "role": "Gathers the material",
                         "system_prompt": "You gather what the task needs and "
                                          "return concise sourced findings.",
                         "toolsets": ["web", "browser"], "reasoning": "medium",
                         "tools": ["web_search", "fetch_url"],
                         "inputs": ["query"], "outputs": ["findings"]},
                        {"name": "writer", "role": "Produces the deliverable",
                         "system_prompt": "You write the final deliverable from "
                                          "the findings you are given.",
                         "toolsets": ["file"], "skills": ["grounded-citations"],
                         "reasoning": "low", "tools": ["read_notes"],
                         "inputs": ["findings"], "outputs": ["document"]},
                    ],
                    "handoff_rules": [
                        {"from": "supervisor", "to": "researcher",
                         "when": "material is needed"},
                        {"from": "researcher", "to": "writer",
                         "when": "findings are ready"},
                    ],
                    "missing_credentials": [], "unsupported_requests": [],
                }
                body = ("Building it now.\n<spec>\n"
                        + json.dumps(spec, indent=2) + "\n</spec>")

            return body + "\n<progress>\n" + json.dumps(progress) + "\n</progress>"

        return f"[mock] {last[:200]}"

    def health(self) -> Dict[str, object]:
        return {"ok": True, "runtime": "mock", "base_url": "in-process",
                "models": ["mock-hermes"]}


_BUDGET_LOCK = threading.RLock()


class BudgetedRuntime:
    """Wraps a runtime with a per-process model-call counter.

    Antigravity subscriptions are quota-limited (weekly and rolling windows), and a
    single team run makes several model calls, so the budget is visible rather than
    discovered when quota runs out.
    """

    def __init__(self, inner, limit_per_run: int = 0) -> None:
        self.inner = inner
        self.name = inner.name
        self.supports_native_tools = getattr(inner, "supports_native_tools", False)
        self.limit_per_run = limit_per_run
        self.total_calls = 0
        self._run_calls = 0
        self.last_error = ""      # surfaced in /api/status
        self.last_ok = False
        self.last_ms = 0
        self.recent_ms: List[int] = []    # last few, to spot a trend

    def begin_run(self) -> None:
        self._run_calls = 0

    def chat(self, messages, **kw) -> str:
        # Workers run concurrently now, so the counter is shared state and a
        # plain read-then-increment would let several calls past the limit.
        with getattr(self, "_lock", _BUDGET_LOCK):
            if self.limit_per_run and self._run_calls >= self.limit_per_run:
                over = True
            else:
                over = False
                self._run_calls += 1
        if over:
            raise RuntimeError(
                f"model-call budget for this run exhausted ({self.limit_per_run} calls)"
            )
        with _BUDGET_LOCK:
            self.total_calls += 1
        t0 = time.monotonic()
        try:
            out = self.inner.chat(messages, **kw)
        except Exception as e:
            self.last_error = str(e)[:600]
            self.last_ok = False
            raise
        finally:
            self.last_ms = int((time.monotonic() - t0) * 1000)
            with _BUDGET_LOCK:
                self.recent_ms.append(self.last_ms)
                del self.recent_ms[:-12]
        self.last_error = ""
        self.last_ok = True
        return out

    def probe(self) -> Dict[str, Any]:
        """Time a trivial call. Whatever it costs is pure overhead — process
        start, config load, plugin init — because the reply is two characters.
        Anything above that is the model actually thinking."""
        t0 = time.monotonic()
        try:
            self.inner.chat([{"role": "user", "content": "reply with: ok"}],
                            reasoning="low", role="interview")
            ok = True
        except Exception:
            ok = False
        return {"ok": ok, "ms": int((time.monotonic() - t0) * 1000)}

    def health(self):
        h = dict(self.inner.health())
        h["calls_this_process"] = self.total_calls
        h["budget_per_run"] = self.limit_per_run or None
        # `--version` succeeding does not prove a real call will: report the
        # last actual call so a broken Hermes is visible in the header.
        h["last_call_ok"] = self.last_ok if self.total_calls else None
        h["last_call_ms"] = self.last_ms or None
        if self.recent_ms:
            h["median_call_ms"] = sorted(self.recent_ms)[len(self.recent_ms) // 2]
        if self.last_error:
            h["last_error"] = self.last_error
            h["ok"] = False
        return h


class HybridRuntime:
    """One transport per role, because they need different things.

    Two roles need only a model, and both go over a persistent HTTP endpoint,
    skipping the ~15s process start a CLI call pays every time:
      interview  — asks questions and designs the team; calls no tools
      supervisor — delegates and merges; its whole prompt is built here, so
                   there is nothing for Hermes to add

    Workers cannot. Hermes is what executes their tools, enforces their `-t`
    grants, preloads their `-s` skills and loads their AGENTS.md via `--in`.
    Sending them to the proxy would hand back raw model output with none of
    that, so they stay on the CLI.

    Each stage may also use a different model: asking questions is cheap work,
    designing a team is not, and delegation sits between the two.
    """
    name = "hybrid"
    VIA_PROXY = {"interview", "design", "supervisor"}

    def __init__(self, fast, full, stage_models=None) -> None:
        self.fast, self.full = fast, full
        self.stage_models = stage_models or {}
        self.supports_native_tools = getattr(full, "supports_native_tools", False)
        self.stage_counts: Dict[str, int] = {}

    def chat(self, messages, *, role: str = "interview", **kw) -> str:
        stage = kw.pop("stage", "") or role
        self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1
        if role in self.VIA_PROXY or stage in self.VIA_PROXY:
            # the proxy takes none of the CLI-only arguments
            for k in ("toolsets", "skills", "workdir", "profile", "session",
                      "cwd"):
                kw.pop(k, None)
            model = self.stage_models.get(stage) or self.stage_models.get(role)
            if model:
                kw["model"] = model
            try:
                return self.fast.chat(messages, role=role, **kw)
            except Exception as e:
                # a dead proxy must not stop the interview
                self._note = f"proxy unavailable, used the CLI: {e}"[:200]
                return self.full.chat(messages, role=role, **kw)
        return self.full.chat(messages, role=role, **kw)

    def health(self) -> Dict[str, Any]:
        h = dict(self.full.health())
        f = self.fast.health()
        h["runtime"] = "hybrid"
        h["interview_transport"] = "proxy" if f.get("ok") else "cli (proxy down)"
        h["proxy"] = {"ok": f.get("ok"), "base_url": f.get("base_url"),
                      "error": f.get("error")}
        if getattr(self, "_note", ""):
            h["note"] = self._note
        h["stage_models"] = self.stage_models
        h["calls_by_stage"] = self.stage_counts
        return h


def build_runtime() -> Runtime:
    transport = os.getenv("HERMES_TRANSPORT",
                          os.getenv("AGENTFORGE_RUNTIME", "cli")).lower()
    budget = int_env("AGENTFORGE_MAX_MODEL_CALLS_PER_RUN", 25)
    fallback = os.getenv("AGENTFORGE_FALLBACK_MOCK", "1") == "1"

    if transport == "mock":
        return BudgetedRuntime(MockHermes(), budget)

    if transport == "http":
        rt = HermesRuntime()
    else:
        rt = HermesCliRuntime()
        # Route just the interview through the proxy when one is configured.
        if os.getenv("HERMES_PROXY_BASE_URL", "").strip():
            default_model = (os.getenv("HERMES_PROXY_MODEL", "") or
                             os.getenv("HERMES_INTERVIEW_MODEL", ""))
            proxy = HermesRuntime(
                base_url=os.getenv("HERMES_PROXY_BASE_URL"),
                model=default_model,
                api_key=(os.getenv("HERMES_PROXY_API_KEY", "")
                         or os.getenv("OLLAMA_API_KEY", "") or "local"),
            )
            stage_models = {
                s: os.getenv(f"HERMES_MODEL_{s.upper()}", "").strip() or default_model
                for s in ("interview", "design", "supervisor")
            }
            rt = HybridRuntime(proxy, rt, stage_models)
    if fallback and not rt.health()["ok"]:
        return BudgetedRuntime(MockHermes(), budget)
    return BudgetedRuntime(rt, budget)
