"""Agent-team orchestration: create the agents from a spec, then run them.

LocalOrchestrator runs the supervisor/sub-agent loop here, calling Hermes once
per agent turn. Each sub-agent is one `hermes -z` invocation carrying its own
toolset grant, skills, reasoning level and working directory, so Hermes executes
the tools and this module only routes work and collects results.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import re
import time
import urllib.request
from typing import Any, Dict, Iterator, List, Protocol

from .. import builder, profiles, store, trust, workspace
from ..envs import int_env
from ..registry import REGISTRY
from ..schema import TeamSpec


def _event(kind: str, agent: str = "", text: str = "", **extra) -> Dict[str, Any]:
    """Every event the run emits passes through here.

    That makes it the one honest place to strip credentials: the stream to
    the browser, the rows written to the trace, the final answer and the
    Multica mirror are all downstream of it, so redacting here cannot be
    bypassed by adding a new caller later.
    """
    clean, removed = trust.redact(text)
    ev = {"kind": kind, "agent": agent, "text": clean, **extra}
    if removed:
        # Say that something was taken out. A silent redaction looks like the
        # agent simply failed to answer.
        ev["redacted"] = removed
    return ev


class Failed(str):
    """A result that did not succeed.

    It behaves as the string it always was, so every caller that formats it
    into a transcript is unaffected, but it carries the one fact a string
    cannot: that this step failed. Checkpointing needs that fact, and reading
    it back out of the text was wrong — a perfectly good answer may begin
    with "[", and a mock backend's output always does.
    """
    __slots__ = ()


class Deadline:
    """A whole run gets a wall-clock budget, not just each call.

    A hierarchy multiplies calls — a supervisor turn delegates to a manager,
    which takes its own turns, each spawning a worker — so a team can grind for
    many minutes while every individual call stays inside its timeout. When the
    budget is spent the run stops cleanly and reports what it has.
    """

    def __init__(self, seconds: int) -> None:
        self.limit = seconds
        self.start = time.monotonic()

    @property
    def spent(self) -> float:
        return time.monotonic() - self.start

    @property
    def left(self) -> float:
        return max(0.0, self.limit - self.spent) if self.limit else 1e9

    def expired(self) -> bool:
        return bool(self.limit) and self.spent >= self.limit


class Orchestrator(Protocol):
    name: str

    def create_team(self, spec: TeamSpec, owner: str = "shared") -> str: ...
    def run(self, spec: TeamSpec, message: str, owner: str = "shared",
            run_id: str = "") -> Iterator[Dict[str, Any]]: ...


# --------------------------------------------------------------------------- local
_TOOL_CALL = re.compile(r"TOOL\s*:\s*(\w+)\s*\(\s*(.*?)\s*\)", re.S)
# The instruction runs to the end of the reply, or to the next DELEGATE/FINAL
# line. It must NOT stop at the first newline: a supervisor routinely writes a
# multi-line instruction ("use these figures:" followed by the figures), and
# truncating it hands the agent a request with its content removed.
_DELEGATE = re.compile(
    r"DELEGATE\s*:\s*(\w+)\s*\|\s*(.+?)(?=\n\s*(?:DELEGATE|FINAL)\s*:|\Z)",
    re.S)
_FINAL = re.compile(r"FINAL\s*:\s*(.+)", re.S)


class LocalOrchestrator:
    name = "local"

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def create_team(self, spec: TeamSpec, owner: str = "shared") -> str:
        """Materialise the agents on disk (role 2) and return the team folder."""
        built = builder.build(spec, owner)
        return str(built["team_dir"])

    def _direct_reports(self, spec: TeamSpec, manager: str = "") -> List:
        """Who this manager may delegate to.

        The top supervisor leads everyone with no manager of their own; a
        managing sub-agent leads exactly the agents it lists. Each level sees
        only its own reports, so a CEO cannot reach past HR to a recruiter.
        """
        by_name = {a.name: a for a in spec.sub_agents}
        if manager:
            return [by_name[n] for n in by_name[manager].manages if n in by_name]
        managed = {n for a in spec.sub_agents for n in a.manages}
        return [a for a in spec.sub_agents if a.name not in managed]

    def _supervisor_system(self, spec: TeamSpec, manager: str = "",
                           role_prompt: str = "", goal: str = "") -> str:
        reports = self._direct_reports(spec, manager)
        roster = "\n".join(
            f"- {a.name}: {a.role}"
            + (f" — leads {', '.join(a.manages)}" if a.manages else
               f" (toolsets: {', '.join(a.toolsets) or 'none'}"
               f"{'; skills: ' + ', '.join(a.skills) if a.skills else ''})")
            for a in reports
        )
        names = {a.name for a in reports}
        rules = "\n".join(
            f"- {r.from_agent} -> {r.to_agent} when {r.when}"
            for r in spec.handoff_rules
            if r.to_agent in names) or "- use your judgement"
        return (
            f"{role_prompt or spec.main_agent.system_prompt}\n\n"
            f"GOAL: {goal or spec.main_agent.goal}\n\n"
            f"Your team:\n{roster}\n\nHandoff guidance:\n{rules}\n\n"
            "Reply with EXACTLY ONE of these lines and nothing else:\n"
            "DELEGATE: <agent_name> | <the full instruction for that agent>\n"
            "FINAL: <your complete answer to the user>\n"
            "Delegate when a teammate is better placed; otherwise answer with FINAL."
        )

    def _files_line(self, owner: str, spec: TeamSpec) -> str:
        """What this agent can actually open, by name.

        A worker starts inside the workspace, so a bare filename resolves —
        but only if it knows the file is there. Being told only by its
        supervisor means a file the supervisor forgot to mention is
        invisible, even though it is sitting in the agent's own directory.
        """
        try:
            names = workspace.listing(workspace.ensure(owner, spec.team_name))
        except Exception:
            return ""
        if not names:
            return ("\n\nThere are no files in your workspace. If the task "
                    "needs one, say so rather than inventing its contents.")
        return ("\n\nFiles in your workspace, openable by name exactly as "
                "written:\n" + "\n".join(f"- {n}" for n in names) +
                "\nUse these names as they are. Do not add a folder path in "
                "front of them.")

    def _worker_system_native(self, spec: TeamSpec, agent,
                              owner: str = "shared") -> str:
        """Hermes runs its own tools, so the prompt describes the job, not a protocol."""
        return (
            f"You are a sub-agent in team '{spec.team_name}'.\n"
            f"ROLE: {agent.role}\n\n{agent.system_prompt}"
            + self._files_line(owner, spec) + "\n\n"
            + trust.data_rule() + "\n"
            "Use the tools and skills available to you as needed, then return your "
            "finished result as plain text. Do not ask clarifying questions — if "
            "something is missing, state the assumption you made and continue."
        )

    def _worker_system(self, spec: TeamSpec, agent) -> str:
        tools = "\n".join(
            f"- {t}: {REGISTRY.get(t).description}" for t in agent.tools if REGISTRY.get(t)
        ) or "- none"
        return (
            f"You are a sub-agent in team '{spec.team_name}'.\n"
            f"ROLE: {agent.role}\n\n{agent.system_prompt}\n\n"
            f"Tools you may call:\n{tools}\n\n"
            "To use a tool, reply with exactly: TOOL: <name>(<single argument>)\n"
            "You will then receive the result and may call another tool or answer.\n"
            "When done, reply with your result as plain text and no TOOL: line."
        )

    def run(self, spec: TeamSpec, message: str,
            owner: str = "shared", run_id: str = "") -> Iterator[Dict[str, Any]]:
        self.deadline = Deadline(int_env("AGENTFORGE_RUN_DEADLINE_S", 900))
        self.run_id = run_id
        sup = spec.main_agent
        ws = str(workspace.ensure(owner, spec.team_name))

        # Host paths are removed before the team ever sees them. A user who
        # pastes one is told, rather than quietly getting a different task
        # than the one they asked for.
        message, removed = workspace.scrub(message)
        if removed:
            yield _event("blocked", sup.name,
                         "Removed from the task, because agents only reach "
                         "this team's workspace: " + "; ".join(removed)
                         + ". Upload the file to the workspace instead.")

        files = workspace.listing(workspace.ensure(owner, spec.team_name))
        here = ("Files in the workspace: " + ", ".join(files)
                if files else "The workspace is empty.")
        convo: List[Dict[str, str]] = [
            {"role": "system", "content": self._supervisor_system(spec)
                                          + "\n\n" + here},
            {"role": "user", "content": message},
        ]
        by_name = {a.name: a for a in self._direct_reports(spec)}

        for turn in range(sup.max_turns_per_run):
            yield _event("thinking", sup.name, f"turn {turn + 1}")
            sup_dir = builder.team_dir(spec.team_name, owner)
            # A supervisor calls no tools: its entire prompt is built above,
            # so it can take the fast transport.
            reply = self.runtime.chat(convo, temperature=0.2,
                                      reasoning=sup.reasoning,
                                      workdir=sup_dir if os.path.isdir(sup_dir) else "",
                                      role="worker", stage="supervisor",
                                      profile=profiles.for_role(owner, "worker") or "",
                                      timeout_s=sup.timeout_s, cwd=ws)

            final = _FINAL.search(reply)
            asks = self._delegations(reply, by_name)

            if asks:
                unknown = [t for t, _ in asks if t not in by_name]
                for t_ in unknown:
                    yield _event("error", sup.name, f"unknown agent '{t_}'")
                asks = [(t_, i) for t_, i in asks if t_ in by_name]
                if not asks:
                    convo.append({"role": "assistant", "content": reply})
                    convo.append({"role": "user",
                                  "content": f"{', '.join(unknown)} not on your "
                                             f"team. Choose from: "
                                             f"{', '.join(by_name)}"})
                    continue

                for t_, instruction in asks:
                    yield _event("delegate", sup.name, instruction, to=t_)
                if len(asks) > 1:
                    yield _event("parallel", sup.name,
                                 f"{len(asks)} agents working at once: "
                                 + ", ".join(t_ for t_, _ in asks))

                done = yield from self._fanout([
                    (f"{n}:{t_}", t_,
                     (lambda a=by_name[t_], i=instruction:
                      self._run_worker(spec, a, i, owner, 1)))
                    for n, (t_, instruction) in enumerate(asks)])

                # Read back in the order delegated, so the supervisor sees a
                # stable transcript however the threads finished.
                merged = "\n\n".join(
                    f"Result from {t_}:\n{done.get(f'{n}:{t_}', '[no result]')}"
                    for n, (t_, _) in enumerate(asks))
                convo.append({"role": "assistant", "content": reply})
                convo.append({"role": "user",
                              "content": merged + "\n\nDelegate again or give FINAL."})
                if unknown:
                    convo[-1]["content"] += (f" ({', '.join(unknown)} is not on "
                                             f"your team.)")
                continue

            answer = final.group(1).strip() if final else reply.strip()
            yield _event("final", sup.name, answer)
            return

        yield _event("final", sup.name,
                     "Stopped: reached the turn limit for this run without a final answer.")

    # ------------------------------------------------------------ fan-out
    def _fanout(self, jobs: List[tuple]):
        """Run several agent generators at once and stream what they say.

        `jobs` is a list of (name, factory), each factory returning the
        generator for one delegation. Events are yielded as they arrive, so
        the interface shows every agent working rather than going quiet; the
        return value is {name: result}, which the caller reads back in the
        order it delegated, so a parallel run and a serial one produce the
        same transcript for the supervisor.

        Threads, not asyncio: the work is `subprocess.run` inside a sync
        generator, so there is nothing to await, and a thread blocked on a
        child process costs nothing.
        """
        if len(jobs) == 1:                     # the common case stays simple
            jid, _name, factory = jobs[0]
            return {jid: (yield from factory())}

        # Two jobs for the same agent cannot overlap: they would share one
        # Hermes profile and one workspace, and the second would race the
        # first over that agent's memory and session files. Same agent runs in
        # sequence; different agents run at once.
        lanes: Dict[str, List[tuple]] = {}
        for jid, name, factory in jobs:
            lanes.setdefault(name, []).append((jid, factory))

        q: "queue.Queue" = queue.Queue()
        results: Dict[str, str] = {}
        gate = threading.Semaphore(max(1, int_env("AGENTFORGE_MAX_PARALLEL", 3)))

        def pump(name: str, lane: List[tuple]) -> None:
            with gate:
                for jid, factory in lane:
                    gen = factory()
                    try:
                        while True:
                            try:
                                q.put((name, next(gen)))
                            except StopIteration as stop:
                                results[jid] = stop.value
                                break
                    except Exception as e:     # one agent failing is not fatal
                        results[jid] = Failed(f"[{name} failed: {e}]")
                        q.put((name, _event("error", name, results[jid])))
                q.put((name, None))            # this lane is finished

        threads = [threading.Thread(target=pump, args=(n, lane), daemon=True,
                                    name=f"af-{n}") for n, lane in lanes.items()]
        for t in threads:
            t.start()

        outstanding = len(threads)
        while outstanding:
            name, ev = q.get()
            if ev is None:
                outstanding -= 1
            else:
                yield ev
        for t in threads:
            t.join(timeout=5)
        return results

    def _delegations(self, reply: str, known) -> List[tuple]:
        """Every delegation in one reply, in order, ignoring any after FINAL.

        The old code took only the first match, so a supervisor that asked two
        agents at once had its second request silently dropped and had to
        spend another turn on it.
        """
        final = _FINAL.search(reply)
        cut = final.start() if final else len(reply)
        out = []
        for m in _DELEGATE.finditer(reply):
            if m.start() >= cut:
                break
            target, instruction = m.group(1).strip(), m.group(2).strip()
            if instruction:
                out.append((target, instruction))
        # The same agent twice in one turn is a duplicate, not two jobs.
        seen, unique = set(), []
        for target, instruction in out:
            if (target, instruction) in seen:
                continue
            seen.add((target, instruction))
            unique.append((target, instruction))
        return unique

    def _run_worker(self, spec: TeamSpec, agent, instruction: str,
                    owner: str = "shared", depth: int = 1):
        # Already done in an earlier attempt at this run? Reuse it rather than
        # paying for the same call twice.
        cached = store.get_step(getattr(self, "run_id", ""), agent.name,
                                instruction)
        if cached is not None:
            yield _event("reused", agent.name,
                         "taken from the earlier attempt, not re-run")
            yield _event("agent_result", agent.name, cached)
            return cached

        if agent.manages:
            result = yield from self._run_manager(spec, agent, instruction,
                                                  owner, depth)
            if result and not isinstance(result, Failed):
                store.save_step(getattr(self, "run_id", ""), agent.name,
                                instruction, result)
            return result
        if getattr(self.runtime, "supports_native_tools", False):
            result = yield from self._run_worker_native(spec, agent,
                                                        instruction, owner)
        else:
            result = yield from self._run_worker_registry(spec, agent,
                                                          instruction)
        # A failed step is not banked: a retry should try it again.
        if result and not isinstance(result, Failed):
            store.save_step(getattr(self, "run_id", ""), agent.name,
                            instruction, result)
        return result

    def _run_manager(self, spec: TeamSpec, manager, instruction: str,
                     owner: str, depth: int):
        """Run a managing agent as a supervisor over its own reports."""
        if depth >= spec.main_agent.max_delegation_depth:
            yield _event("error", manager.name,
                         f"delegation depth limit ({spec.main_agent.max_delegation_depth}) "
                         f"reached; answering directly instead of delegating")
            return (yield from self._run_worker_native(spec, manager, instruction, owner)
                    if getattr(self.runtime, "supports_native_tools", False)
                    else self._run_worker_registry(spec, manager, instruction))

        reports = {a.name: a for a in self._direct_reports(spec, manager.name)}
        yield _event("manages", manager.name, ", ".join(reports))
        dl = getattr(self, "deadline", None)
        convo = [
            {"role": "system",
             "content": self._supervisor_system(spec, manager.name,
                                                manager.system_prompt,
                                                manager.role)},
            {"role": "user", "content": instruction},
        ]
        workdir = builder.agent_dir(spec.team_name, manager.name, owner)
        for turn in range(spec.main_agent.max_turns_per_run):
            if dl and dl.expired():
                out = Failed(f"[{manager.name} stopped: the run reached its "
                             f"time budget before this branch finished]")
                yield _event("error", manager.name, out)
                return out
            # Without this the interface shows nothing at all while a manager
            # works through its own reports, which reads as a hang.
            yield _event("thinking", manager.name,
                         f"turn {turn + 1} of {spec.main_agent.max_turns_per_run}"
                         + (f" · {dl.spent:.0f}s elapsed" if dl else ""))
            reply = self.runtime.chat(
                convo, temperature=0.2, reasoning=manager.reasoning,
                workdir=workdir if os.path.isdir(workdir) else "",
                role="worker", stage="supervisor",
                profile=profiles.for_role(owner, "worker") or "",
                timeout_s=manager.timeout_s, model=manager.model,
                provider=manager.provider,
                cwd=str(workspace.ensure(owner, spec.team_name)))
            final = _FINAL.search(reply)
            asks = self._delegations(reply, reports)
            if asks:
                unknown = [t_ for t_, _ in asks if t_ not in reports]
                asks = [(t_, i) for t_, i in asks if t_ in reports]
                if not asks:
                    convo += [{"role": "assistant", "content": reply},
                              {"role": "user",
                               "content": f"{', '.join(unknown)} do not report "
                                          f"to you. Choose from: "
                                          f"{', '.join(reports)}"}]
                    continue
                for t_, sub_instruction in asks:
                    yield _event("delegate", manager.name, sub_instruction, to=t_)
                if len(asks) > 1:
                    yield _event("parallel", manager.name,
                                 f"{len(asks)} agents working at once: "
                                 + ", ".join(t_ for t_, _ in asks))
                done = yield from self._fanout([
                    (f"{n}:{t_}", t_,
                     (lambda a=reports[t_], i=sub_instruction:
                      self._run_worker(spec, a, i, owner, depth + 1)))
                    for n, (t_, sub_instruction) in enumerate(asks)])
                merged = "\n\n".join(
                    f"Result from {t_}:\n{done.get(f'{n}:{t_}', '[no result]')}"
                    for n, (t_, _) in enumerate(asks))
                convo += [{"role": "assistant", "content": reply},
                          {"role": "user",
                           "content": merged + "\n\nDelegate again or give FINAL."}]
                continue
            answer = (final.group(1) if final else reply).strip()
            yield _event("agent_result", manager.name, answer)
            return answer
        out = Failed(f"[{manager.name} hit its turn limit]")
        yield _event("agent_result", manager.name, out)
        return out

    def _run_worker_native(self, spec: TeamSpec, agent, instruction: str,
                           owner: str = "shared"):
        """One Hermes call. Toolsets and skills are enforced by the runtime, not
        requested in the prompt, so the agent cannot reach past its grant."""
        if agent.model_tier == "cheap" and profiles.cheap_available():
            yield _event("model_tier", agent.name,
                         f"cheap · {profiles.CHEAP_PROVIDER}/{profiles.CHEAP_MODEL}")
        if agent.toolsets:
            yield _event("tool_grant", agent.name, ", ".join(agent.toolsets))
        if agent.skills:
            yield _event("skill_grant", agent.name, ", ".join(agent.skills))
        workdir = builder.agent_dir(spec.team_name, agent.name, owner)
        try:
            out = self.runtime.chat(
                [{"role": "system",
                  "content": self._worker_system_native(spec, agent, owner)},
                 {"role": "user", "content": instruction}],
                toolsets=agent.toolsets or None,
                skills=agent.skills or None,
                reasoning=agent.reasoning,
                workdir=workdir if os.path.isdir(workdir) else "",
                role="worker",
                profile=profiles.for_role(owner, "worker", agent.model_tier) or "",
                timeout_s=agent.timeout_s,
                model=agent.model, provider=agent.provider,
                cwd=str(workspace.ensure(owner, spec.team_name)),
            ).strip()
        except Exception as e:
            out = Failed(f"[{agent.name} failed: {e}]")
            yield _event("error", agent.name, out)
            return out
        leaked = workspace.escapes(out)
        if leaked:
            yield _event("blocked", agent.name,
                         "This agent named a location outside its workspace: "
                         + "; ".join(leaked))
        # Did something it read talk it into reaching for a secret?
        for f in trust.followed_injection(out):
            yield _event("blocked", agent.name,
                         f"{f['why']} — check what this agent read: "
                         f"{f['quote']}")
        yield _event("agent_result", agent.name, out)
        return out

    def _run_worker_registry(self, spec: TeamSpec, agent, instruction: str):
        convo = [
            {"role": "system", "content": self._worker_system(spec, agent)},
            {"role": "user", "content": instruction},
        ]
        last = ""
        for _ in range(4):
            reply = self.runtime.chat(convo, temperature=0.3)
            call = _TOOL_CALL.search(reply)
            if not call:
                yield _event("agent_result", agent.name, reply.strip())
                return reply.strip()
            tool, arg = call.group(1), call.group(2).strip().strip("\"'")
            if tool not in agent.tools:
                out = f"[{tool} is not in your allowed tools: {', '.join(agent.tools)}]"
            else:
                yield _event("tool_call", agent.name, f"{tool}({arg[:120]})", tool=tool)
                out = REGISTRY.call(tool, arg)
                yield _event("tool_result", agent.name, out[:400], tool=tool)
            convo.append({"role": "assistant", "content": reply})
            convo.append({"role": "user", "content": f"Tool result:\n{out[:4000]}"})
            last = out
        summary = f"(tool budget spent) last tool output:\n{last[:1000]}"
        yield _event("agent_result", agent.name, summary)
        return summary


def build_orchestrator(runtime):
    """Only one orchestrator now.

    An earlier version had a MulticaOrchestrator that handed teams to Multica
    over HTTP. It is gone: Multica executes through its own daemon on its own
    runtimes, so routing teams there would mean two orchestrators driving the
    same work. Multica is now a tracking surface only — see backend/mirror.py.
    """
    return LocalOrchestrator(runtime)
