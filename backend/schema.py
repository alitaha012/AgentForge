"""Framework-neutral agent-team spec. Everything downstream reads only this."""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


REASONING_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
# "cheap" runs on the small local model (free, on-machine, weaker at protocol
# and tools); "capable" runs on the configured main model.
MODEL_TIERS = {"cheap", "capable"}


class SubAgent(BaseModel):
    name: str                      # the stable id used for delegation
    nickname: str = ""             # what the user calls it; display only
    role: str
    system_prompt: str
    # How it should come across. Voice and manner only — never what it does,
    # which belongs in system_prompt.
    personality: str = ""
    # Its own memory file, kept beside its AGENTS.md and carried between runs.
    memory_enabled: bool = True
    # Hermes toolsets enforced per invocation via `-t` (not a prompt-level request).
    toolsets: List[str] = Field(default_factory=list)
    # Hermes skills preloaded per invocation via `-s`.
    skills: List[str] = Field(default_factory=list)
    reasoning: str = "medium"
    # Seconds this agent may take. 0 = use HERMES_CLI_TIMEOUT. A web-research
    # agent needs far longer than one that reformats text.
    timeout_s: int = 0
    # Optional per-agent model/provider override; empty means use the role
    # default from the environment.
    model: str = ""
    provider: str = ""
    model_tier: str = "capable"
    # Names of other sub-agents this one leads. An agent with reports is a
    # manager: it delegates to them instead of doing the work itself, which is
    # what turns a flat team into an org chart (CEO -> HR -> recruiter).
    manages: List[str] = Field(default_factory=list)
    # In-process registry tools, used only when the runtime has no native tools.
    tools: List[str] = Field(default_factory=list)
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)

    @field_validator("reasoning")
    @classmethod
    def level(cls, v: str) -> str:
        v = (v or "medium").strip().lower()
        return v if v in REASONING_LEVELS else "medium"

    @field_validator("model_tier")
    @classmethod
    def tier(cls, v: str) -> str:
        v = (v or "capable").strip().lower()
        return v if v in MODEL_TIERS else "capable"

    @field_validator("name")
    @classmethod
    def slug(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "_").replace("-", "_")
        if not v.isidentifier():
            raise ValueError(f"agent name not usable as an identifier: {v!r}")
        return v

    @field_validator("nickname", "personality")
    @classmethod
    def trim(cls, v: str) -> str:
        return (v or "").strip()[:400]

    @property
    def display(self) -> str:
        return self.nickname or self.name


class MainAgent(BaseModel):
    name: str = "supervisor"
    nickname: str = ""
    goal: str
    system_prompt: str
    personality: str = ""
    memory_enabled: bool = True
    routing_policy: str = "delegate_by_capability"
    reasoning: str = "high"
    timeout_s: int = 0
    max_delegation_depth: int = 2
    max_turns_per_run: int = 8

    @field_validator("reasoning")
    @classmethod
    def level(cls, v: str) -> str:
        v = (v or "high").strip().lower()
        return v if v in REASONING_LEVELS else "high"


class HandoffRule(BaseModel):
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    when: str

    model_config = {"populate_by_name": True}


class TeamSpec(BaseModel):
    team_name: str
    summary: str = ""
    main_agent: MainAgent
    sub_agents: List[SubAgent] = Field(default_factory=list)
    handoff_rules: List[HandoffRule] = Field(default_factory=list)
    missing_credentials: List[str] = Field(default_factory=list)
    unsupported_requests: List[str] = Field(default_factory=list)

    @field_validator("team_name")
    @classmethod
    def slug(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "_").replace("-", "_")


class ValidationIssue(BaseModel):
    level: str  # "error" | "warning"
    message: str


def org_errors(spec: "TeamSpec") -> List[str]:
    """Check the management tree: every edge real, one boss each, no cycles,
    and no deeper than the supervisor allows."""
    errs: List[str] = []
    names = {a.name for a in spec.sub_agents}
    by_name = {a.name: a for a in spec.sub_agents}

    boss_of: dict = {}
    for a in spec.sub_agents:
        for r in a.manages:
            if r == a.name:
                errs.append(f"{a.name} manages itself")
            elif r not in names:
                errs.append(f"{a.name} manages unknown agent '{r}'")
            elif r in boss_of:
                errs.append(f"{r} is managed by both {boss_of[r]} and {a.name}")
            else:
                boss_of[r] = a.name

    # Depth from the top, and cycle detection in one walk.
    tops = [a.name for a in spec.sub_agents if a.name not in boss_of]
    if spec.sub_agents and not tops:
        errs.append("every agent has a manager, so the chart is a cycle")
    seen_depth: dict = {}

    def walk(name: str, depth: int, path: tuple) -> None:
        if name in path:
            errs.append("management cycle: " + " -> ".join(path + (name,)))
            return
        seen_depth[name] = max(seen_depth.get(name, 0), depth)
        for r in by_name[name].manages:
            if r in by_name:
                walk(r, depth + 1, path + (name,))

    for top in tops:
        walk(top, 1, ())

    limit = spec.main_agent.max_delegation_depth
    too_deep = [n for n, d in seen_depth.items() if d > limit]
    if too_deep:
        errs.append(f"{', '.join(sorted(too_deep))} sit deeper than "
                    f"max_delegation_depth ({limit}); raise it or flatten")
    unreached = names - set(seen_depth)
    if unreached:
        errs.append(f"unreachable agents: {', '.join(sorted(unreached))}")
    return errs


def validate_spec(spec: TeamSpec, registry) -> List[ValidationIssue]:
    """Structural + registry validation. Errors block the build; warnings don't."""
    issues: List[ValidationIssue] = []

    if not spec.sub_agents:
        issues.append(ValidationIssue(level="error", message="team has no sub-agents"))

    for msg in org_errors(spec):
        issues.append(ValidationIssue(level="error", message=msg))

    # Reading outside material and running commands on one agent is the
    # combination an injected line needs, so it is surfaced at design time.
    issues.extend(grant_warnings(spec))

    names = [a.name for a in spec.sub_agents]
    for n in set(names):
        if names.count(n) > 1:
            issues.append(ValidationIssue(level="error", message=f"duplicate sub-agent name: {n}"))
    if spec.main_agent.name in names:
        issues.append(
            ValidationIssue(level="error", message="main agent shares a name with a sub-agent")
        )

    known = set(registry.tool_names())
    known_toolsets = set(registry.toolset_names())
    for a in spec.sub_agents:
        for t in a.tools:
            if t not in known:
                issues.append(
                    ValidationIssue(
                        level="error", message=f"{a.name} requests unknown tool '{t}'"
                    )
                )
        for ts in a.toolsets:
            if known_toolsets and ts not in known_toolsets:
                issues.append(
                    ValidationIssue(
                        level="error",
                        message=f"{a.name} requests unknown Hermes toolset '{ts}'",
                    )
                )
        if a.memory_enabled and a.toolsets and "file" not in a.toolsets:
            issues.append(ValidationIssue(
                level="warning",
                message=f"{a.name} has its own memory but no 'file' toolset, so "
                        f"it cannot read or write it"))
        if a.manages and a.model_tier == "cheap":
            issues.append(ValidationIssue(
                level="error",
                message=f"{a.name} leads other agents, so it cannot run on the "
                        f"cheap tier: a small model breaks the delegation protocol"))
        if a.manages and a.toolsets:
            issues.append(ValidationIssue(
                level="warning",
                message=f"{a.name} manages {len(a.manages)} agents but also holds "
                        f"toolsets; managers should delegate, not do the work"))
        if len(a.system_prompt) < 30:
            issues.append(
                ValidationIssue(level="warning", message=f"{a.name} has a very thin system prompt")
            )

    for a in spec.sub_agents:
        if a.timeout_s and a.timeout_s > 3600:
            issues.append(ValidationIssue(
                level="error", message=f"{a.name} timeout_s above one hour"))
        if a.toolsets and {"web", "browser"} & set(a.toolsets) and \
                0 < a.timeout_s < 300:
            issues.append(ValidationIssue(
                level="warning",
                message=f"{a.name} does web work with only {a.timeout_s}s"))

    for r in spec.handoff_rules:
        for side in (r.from_agent, r.to_agent):
            if side != spec.main_agent.name and side not in names:
                issues.append(
                    ValidationIssue(
                        level="error", message=f"handoff rule references unknown agent '{side}'"
                    )
                )

    for cred in registry.missing_credentials(
        {t for a in spec.sub_agents for t in a.tools}
    ):
        issues.append(
            ValidationIssue(level="warning", message=f"credential not configured: {cred}")
        )

    if spec.main_agent.max_delegation_depth > 5:
        issues.append(
            ValidationIssue(level="error", message="max_delegation_depth above 4 risks loops")
        )

    return issues


def grant_warnings(spec: "TeamSpec") -> List[ValidationIssue]:
    """Capability combinations that make an injection serious.

    Raised as warnings rather than errors by default: a data analyst that
    both reads a file and runs code is a legitimate design, and refusing to
    build it would push people to turn the check off entirely. Set
    AGENTFORGE_STRICT_GRANTS=1 to make them refusals instead.
    """
    import os
    from .trust import grant_conflicts
    level = "error" if os.getenv("AGENTFORGE_STRICT_GRANTS", "0") == "1" \
        else "warning"
    out: List[ValidationIssue] = []
    for a in [spec.main_agent, *spec.sub_agents]:
        for msg in grant_conflicts(a):
            out.append(ValidationIssue(level=level, message=msg))
    return out


def remove_agent(spec: "TeamSpec", name: str) -> List[str]:
    """Take one sub-agent out of a team and repair everything that named it.

    Deleting an agent is never just a list removal. Something may manage it,
    it may manage others, and handoff rules may route through it. Left alone,
    each of those becomes a validation error or, worse, a silent orphan that
    never gets any work.

    The repairs, in order:
      - its reports are promoted to its own manager, so a branch is not lost
        along with its middle layer; with no manager above, they become
        top-level reports of the supervisor
      - every `manages` list that mentioned it drops the entry
      - handoff rules that start or end at it are removed

    Mutates `spec` and returns a plain-language note for each repair, so the
    person is told what else changed rather than discovering it later.
    """
    victim = next((a for a in spec.sub_agents if a.name == name), None)
    if victim is None:
        raise KeyError(name)

    notes: List[str] = []
    boss = next((a for a in spec.sub_agents if name in a.manages), None)

    if victim.manages:
        orphans = list(victim.manages)
        if boss is not None:
            for r in orphans:
                if r not in boss.manages:
                    boss.manages.append(r)
            notes.append(f"{', '.join(orphans)} now report to {boss.name}")
        else:
            notes.append(f"{', '.join(orphans)} now report to "
                         f"{spec.main_agent.name} directly")

    for a in spec.sub_agents:
        if name in a.manages:
            a.manages = [r for r in a.manages if r != name]

    dropped = [r for r in spec.handoff_rules
               if r.from_agent == name or r.to_agent == name]
    if dropped:
        spec.handoff_rules = [r for r in spec.handoff_rules if r not in dropped]
        notes.append(f"{len(dropped)} handoff rule(s) naming {name} removed")

    spec.sub_agents = [a for a in spec.sub_agents if a.name != name]
    return notes
