# Which model runs which stage

AgentForge makes four different kinds of model call. They have almost
nothing in common, so paying one price for all four is waste at one end
and a quality cap at the other.

| stage | what it does | tools? | transport | model |
|---|---|---|---|---|
| `interview` | asks the 7 questions, writes `<progress>` | no | HTTP proxy | `gpt-oss:20b-cloud` |
| `design` | turns the transcript into the team spec JSON | no | HTTP proxy | `gpt-oss:120b-cloud` |
| `supervisor` | reads the roster, emits `DELEGATE:` / `FINAL:` | no | HTTP proxy | `gpt-oss:120b-cloud` |
| `worker` | the agents themselves | **yes** | Hermes CLI | profile (`-w` Claude / `-wc` cheap) |

## Why these

**Asking questions is the cheapest work in the app and the most frequent.**
Seven turns minimum, each one short: a question plus a progress block. It
needs instruction-following, not intelligence. `gpt-oss:20b` has the
cheapest output in the catalogue ($0.30/M) and a 131K context that the
transcript will never fill.

**Designing the team is the one call where being wrong is expensive.**
It emits the JSON that every later stage consumes: a bad org chart means a
run that fails validation or, worse, one that runs and produces nonsense.
`gpt-oss:120b` is the strongest model here that still costs under a fifth
of `glm-5.3`, and its reasoning effort is adjustable, which is what
`DESIGN_REASONING=high` needs. If a spec ever comes back malformed,
`HERMES_MODEL_DESIGN=glm-5.3` is the upgrade — $1.40/$4.40, one call per
team, so a few cents.

**Supervisors are a protocol, not a conversation.** They have to pick a
worker and write one line in a fixed shape, repeatedly, without drifting.
That rewards reliability over brilliance, and it is the stage where a weak
model hurts most subtly: it answers the user's question itself instead of
delegating. `gpt-oss:120b` again.

**Workers cannot move.** Hermes is what executes their tools, enforces
their `-t` grants, preloads their `-s` skills and loads their AGENTS.md.
The model is whatever their profile points at, so it is set with
`HERMES_CHEAP_MODEL` / the `-w` profile, not here.

## The speed win

Three of the four stages now skip the Hermes process start. Measured on
this machine: **overhead 14.7s per CLI call**, median turn 27.1s. Every
interview turn, every design turn and every supervisor turn drops that
14.7s. A supervisor that delegates to four workers made five CLI calls
before this change and makes four now, and the four that remain are the
only ones that ever needed a process.

Only workers still pay startup, and they pay it because they are the only
stage that actually uses what the process provides.

## Privacy, stated plainly

Cloud models send the team name, the interview transcript, the spec and
every supervisor prompt to Ollama's servers. That is a real departure from
"everything local", which is what you asked for at the start. The trade is
speed and quality against locality, and it is yours to make, not mine.

Two ways back:
- `HERMES_MODEL_*` pointed at a local tag keeps that stage on your machine.
  On this laptop that means CPU inference, which is why it is slow today.
- Leaving `HERMES_PROXY_BASE_URL` unset puts every stage back on the Hermes
  CLI and your Claude subscription. Slower, fully private.

Workers on the `-w` profile already run on Claude, not Ollama.
