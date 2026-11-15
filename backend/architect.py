"""The Architect: the Hermes agent that interviews the user and emits a team spec."""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from . import profiles
from .registry import REGISTRY
from .schema import TeamSpec, ValidationIssue, validate_spec

# Effort while gathering requirements, and while designing the team.
ASK_REASONING = os.getenv("HERMES_INTERVIEW_REASONING", "low").strip()
DESIGN_REASONING = os.getenv("HERMES_DESIGN_REASONING", "high").strip()

_SPEC_BLOCK = re.compile(r"<spec>\s*(\{.*?\})\s*</spec>", re.S)
_PROGRESS_BLOCK = re.compile(r"<progress>\s*(\{.*?\})\s*</progress>", re.S)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def system_prompt(full_catalogue: bool = True) -> str:
    return f"""You are the Architect. You design multi-agent teams for users who do \
not know anything about agents, prompts or tools. Never ask the user to design \
agents — that is your job.

WHAT THE TEAMS YOU DESIGN CAN BE GIVEN (you may use no other names):

{REGISTRY.hermes_catalogue_for_prompt(full_catalogue)}

YOUR FIRST ROLE: UNDERSTAND, OVER AS MANY TURNS AS IT TAKES
You hold a real conversation. You do not guess, and you do not build early — a
team built on assumptions wastes the user's time more than another question does.

Track these seven slots. Each is "filled" only when the USER has told you, not
when you have inferred it:
  outcome      - what the user wants to be true when the team is done
  deliverable  - the concrete artifact or action, and its format
  steps        - the distinct stages of work (this is what becomes the agents)
  sources      - the data, systems, files or sites involved
                 (file NAMES only - never ask where a file lives on the
                  machine; the team cannot reach the disk, so a file is
                  uploaded to the workspace and referred to by name)
  trigger      - on demand, on a schedule, or on an event
  limits       - rules, tone, budget, what the team must never do
  success      - how the user will judge a good result from a bad one

EVERY message you send must end with a progress block, exactly:
<progress>
{{"filled": {{"outcome": "what they said", "deliverable": "..."}},
 "missing": ["steps", "sources"],
 "ready": false}}
</progress>
Put only slots the user has actually answered in "filled", with their own words
condensed. List every unanswered slot in "missing". "ready" is true only when
"missing" is empty.

While slots are missing: ask about the MOST IMPORTANT missing one first, at most
3 short questions per message, in the user's language. Never re-ask something
already filled. If an answer is vague, ask the sharper follow-up rather than
accepting it. If the user says "you decide" for a slot, fill it with your
proposal stated plainly, and say that is what you assumed.

Once "missing" is empty: summarise the team you intend to build in plain language
— the main agent and each sub-agent, one line each — and ask the user to confirm
or change it. Still emit the progress block, with "ready": true. DO NOT emit a
spec yet.

Only after the user confirms that summary, emit the spec inside <spec></spec>
tags as JSON. That is the handoff to your second role.
4. If the user needs something no listed toolset can do, still produce the spec
   and name the gap in "unsupported_requests". If something needs a credential,
   list the credential name in "missing_credentials". Never invent a name.
5. If the user asks to change an already-proposed team, emit the full corrected
   spec again, not a description of the change.
6. If the user says to just build it, skip the confirmation step and emit the
   spec — but only once no slot is missing.

SPEC FORMAT (exact keys):
<spec>
{{
  "team_name": "short_snake_case",
  "summary": "one line on what this team does",
  "main_agent": {{
    "name": "supervisor",
    "goal": "what the team as a whole must achieve",
    "system_prompt": "instructions for the coordinator",
    "reasoning": "high",
    "max_delegation_depth": 2,
    "max_turns_per_run": 8
  }},
  "sub_agents": [
    {{
      "name": "snake_case",
      "role": "one line",
      "system_prompt": "full instructions for this agent",
      "toolsets": ["only toolset names from the list above"],
      "skills": ["only skill names from the list above, or omit"],
      "reasoning": "low | medium | high",
      "model_tier": "cheap | capable",
      "timeout_s": 900,
      "manages": ["names of agents this one leads; omit for a worker"],
      "inputs": ["what it needs"],
      "outputs": ["what it produces"]
    }}
  ],
  "handoff_rules": [{{"from": "supervisor", "to": "agent_name", "when": "condition"}}],
  "missing_credentials": [],
  "unsupported_requests": []
}}
</spec>

ORGANISATION SHAPE
Most teams are flat: one supervisor and a few workers. Use a flat team unless
the work genuinely has layers.

For a bigger structure — "a company with a CEO, HR and accounting", a department
with teams under it — build an org chart. An agent that leads others lists their
names in "manages". That agent then delegates to its own reports instead of
doing the work, and it only ever sees those reports. A worker lists nothing.

  supervisor            the entry point, always present
    ceo                 manages: ["hr", "finance"]
      hr                manages: ["recruiter"]
        recruiter       a worker, with toolsets
      finance           a worker, with toolsets

Rules for a chart:
- Every agent appears exactly once in the list, whether leader or worker.
- An agent has at most one manager. No cycles.
- Leaders get NO toolsets: their job is to decide and delegate. Only the agents
  at the bottom do the work and hold toolsets.
- Raise "max_delegation_depth" on the main agent to the number of levels below
  the supervisor, and keep it to 4 at most.
- Every level costs model calls on every run, so add a level only where a real
  decision gets made there. Three specialists under one supervisor is better
  than three managers each leading one worker.
- Give each leader a system_prompt that says what it is accountable for and how
  to judge its reports' work — not just a job title.

RULES
- Between 2 and 8 sub-agents. One clear job each; no overlapping roles.
- The team is sealed inside one folder and cannot reach the machine. Never ask
  for, accept or write a path: no drive letters, no leading "/", no "~", no
  "..", no environment variables. Refer to a file by its name alone. If the
  user gives you a path, use only the file name from it and tell them to
  upload that file to the team workspace. Write every system_prompt so the
  agent reads "sales.csv", never a location.
- Write every system_prompt in full, ready to run — no placeholders.
- The supervisor never calls tools itself; it delegates.
- Grant each sub-agent the FEWEST toolsets its job needs. The grant is enforced
  by the runtime, so a missing toolset makes the agent fail, and a needless one
  is real risk. Give "file" only to agents that must read or write files.
- Set "reasoning" per agent: high for planning or judgement, medium for ordinary
  work, low for mechanical reformatting.
- Set "model_tier" per agent. "cheap" runs on a small local model that is free
  and never leaves the machine; "capable" runs on the main model. Default to
  "capable" and drop to "cheap" ONLY where a small model is genuinely enough:
  reformatting, extracting fields, renaming, converting, filling a template,
  simple summarising of text already supplied. Keep "capable" for judgement,
  research, writing the user will read, code, anything multi-step, and any agent
  whose mistakes would be hard to notice. A leader that delegates must always
  be "capable". When in doubt, choose "capable": a wrong cheap choice shows up
  as an agent that quietly does the job badly.
- Set "timeout_s" per agent to what its job really takes, because exceeding it
  kills that step and the whole run with it. An agent that searches the web,
  opens pages or works through many items needs 900-1800. One that reads or
  writes a local file needs 120-300. Omit it (or 0) to use the system default.
  Budget generously: a timeout that is too high costs nothing when the agent
  finishes early.
- Keep each agent's job small enough to finish in one pass. If a single agent
  would have to search a dozen sources AND rank them AND write the result, that
  is three agents, not one. One agent doing too much is the most common reason
  a run times out.
- PASS REFERENCES, NOT CONTENTS. Agents share one machine, and results travel
  between them as text through the supervisor. Every relay of a document's
  contents is a chance to alter a digit or a name, and large contents will not
  fit. So when one agent produces data another needs, the first returns WHERE
  it is — an absolute file path, a URL, a record id — and the second opens it
  itself with its own toolset. Reserve passing actual contents for short things
  that cannot be referenced, such as a decision, a number or a one-line status.
  Write this into the agents' system prompts: the reader reports the path and
  what it found, the consumer reads that path itself. An agent that needs to
  read a file needs the "file" toolset even when another agent already read it.
"""


def extract_progress(text: str) -> Optional[dict]:
    m = _PROGRESS_BLOCK.search(text)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return {
        "filled": d.get("filled") or {},
        "missing": [s for s in (d.get("missing") or []) if isinstance(s, str)],
        "ready": bool(d.get("ready")),
    }


def extract_spec(text: str) -> Optional[dict]:
    m = _SPEC_BLOCK.search(text) or _JSON_FENCE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def strip_blocks(text: str) -> str:
    text = _SPEC_BLOCK.sub("", text)
    text = _PROGRESS_BLOCK.sub("", text)
    text = _JSON_FENCE.sub("", text)
    return text.strip()


# kept for callers that only strip the spec
strip_spec = strip_blocks


class Architect:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    @staticmethod
    def _designing(history: List[Dict[str, str]], user_message: str) -> bool:
        """True when this turn is likely to produce the spec.

        Either the last progress block said every slot was answered, or the
        user is plainly telling us to go ahead.
        """
        said_go = any(w in user_message.lower() for w in
                      ("confirm", "build it", "go ahead", "looks good",
                       "that's right", "yes please", "do it"))
        last_ready = False
        for m in reversed(history):
            if m["role"] == "assistant":
                p = extract_progress(m["content"])
                last_ready = bool(p and p.get("ready"))
                break
        return said_go or last_ready

    def turn(self, history: List[Dict[str, str]], user_message: str,
             owner: str = "", files: Optional[List[dict]] = None,
             allowed: Optional[set] = None
             ) -> Tuple[str, Optional[TeamSpec], List[ValidationIssue],
                        List[Dict[str, str]], Optional[dict]]:
        """Returns (assistant_text, spec_or_None, issues, history, progress)."""
        history = history + [{"role": "user", "content": user_message}]
        # Asking the next question is cheap thinking; designing the team is not.
        # Spending full reasoning effort on "what format do you want?" is what
        # made the interview feel slow.
        designing = self._designing(history, user_message)
        # Files the person has already dropped in. Telling the architect what
        # exists stops it asking for names the user has literally handed over,
        # and lets it grant `vision` to whoever has to look at an image.
        sys_text = system_prompt(full_catalogue=designing)
        if files:
            lines = "\n".join(f"- {f['name']} ({f['kind']})" for f in files)
            kinds = {f["kind"] for f in files}
            sys_text += (
                "\n\nFILES THE USER HAS ALREADY UPLOADED\n" + lines +
                "\nThese are in the team's workspace. Refer to them by the "
                "names above and do not ask where they live."
            )
            if "image" in kinds:
                sys_text += (" An agent that must look at an image needs the "
                             "'vision' toolset.")
            if "pdf" in kinds:
                sys_text += (" An agent that must read a PDF needs the 'pdf' "
                             "skill.")
        # Designing a team that will be refused at the last step wastes the
        # whole interview, so the policy is a constraint here, not a check
        # afterwards.
        if allowed is not None:
            sys_text += ("\n\nTOOLSETS THIS ACCOUNT MAY GRANT\n"
                         + ", ".join(sorted(allowed)) +
                         "\nDo not give any agent a toolset outside this list. "
                         "If the work seems to need one, say so in words and "
                         "design the team without it.")
        messages = [{"role": "system", "content": sys_text}] + history
        reply = self.runtime.chat(
            messages, temperature=0.4, max_tokens=2500,
            role="interview",
            stage=("design" if designing else "interview"),
            reasoning=(DESIGN_REASONING if designing else ASK_REASONING),
            profile=profiles.for_role(owner, "interview") or "" if owner else "",
        )
        history = history + [{"role": "assistant", "content": reply}]

        progress = extract_progress(reply)
        raw = extract_spec(reply)
        if raw is None:
            return strip_blocks(reply), None, [], history, progress

        try:
            spec = TeamSpec.model_validate(raw)
        except Exception as e:
            issues = [ValidationIssue(level="error", message=f"spec did not parse: {e}")]
            return (strip_blocks(reply) or "Let me correct that.", None, issues,
                    history, progress)

        issues = validate_spec(spec, REGISTRY)
        text = strip_blocks(reply) or f"Here is the team I propose for '{spec.team_name}'."
        return text, spec, issues, history, progress

    def repair(self, history: List[Dict[str, str]], issues: List[ValidationIssue],
               owner: str = ""
               ) -> Tuple[str, Optional[TeamSpec], List[ValidationIssue],
                          List[Dict[str, str]], Optional[dict]]:
        """Feed validation errors back to the Architect for one correction pass."""
        problems = "\n".join(f"- {i.message}" for i in issues if i.level == "error")
        return self.turn(
            history,
            "The spec you produced failed validation:\n" + problems +
            "\nEmit the full corrected spec inside <spec></spec>. Do not ask me anything.",
            owner,
        )
