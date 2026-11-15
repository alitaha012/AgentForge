# AgentForge — design brief prompt

Paste this whole file as the prompt. It describes the platform, every screen,
and the exact design system, so the result matches what already exists instead
of drifting into a generic AI-tool look.

---

You are designing the interface for **AgentForge**, a local web application.

## What the product is

A person describes a piece of work in plain words. An interviewer agent asks
until the job is clear, then builds a team of AI agents to do it — a
supervisor, managers, and workers under them. The person then talks to that
team and watches it work.

Everything runs on the user's own machine. Teams are private per account.
Agents are sealed inside one folder and cannot touch the rest of the disk.

Three modes: **Build** (the interview), **Run** (talking to a live team), and
**Admin** (oversight of every user's teams). A sign-in page sits in front.

## The feeling to aim for

Warm, soft, calm, and unmistakably a workshop rather than a dashboard. The
user is commissioning colleagues, not configuring software. Paper and ink, not
glass and neon. Nothing should feel like a control panel or a telemetry
console.

Hold that warmth even where the content is technical. A failed run is a
setback, not an alarm.

## Design tokens — use these exact values

### Colour, light

```
--paper:#F8F5EF   page               --surface:#FFFDFA  cards, inputs
--sunk:#F0EBE2    wells, hover
--ink:#241F1A     primary text       --ink-soft:#4A423A secondary
--muted:#7B7065   labels             --faint:#A89D90    hints, disabled
--rule:#E8E1D6    borders            --rule-strong:#D6CCBD emphasis
--accent:#3E6B7A  one teal accent
```

State colours, used only for state, never decoration:

```
--running:#4E7A5E  green, working    --pending:#A9772B  amber, waiting
--fault:#A8484A    red, failed       --quiet:#A89D90    grey, idle
```

### Colour, dark (`prefers-color-scheme: dark`)

```
--paper:#1A1714  --surface:#221E1A  --sunk:#141210
--ink:#F0EBE3    --ink-soft:#CFC5B8 --muted:#9A8F82  --faint:#73695F
--rule:#312B25   --rule-strong:#473F37 --accent:#8FBCCB
--running:#7FB88F --pending:#D6A75C --fault:#DE8784  --quiet:#73695F
```

Both themes ship. Dark is not an afterthought; the warm cast is kept in it —
the dark paper is brown-black, never blue-black or pure `#000`.

### Type

```
--serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif
--sans:  "Inter", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif
--mono:  "Cascadia Mono", "SF Mono", "Consolas", ui-monospace, monospace
```

Serif carries anything an agent or the user *says* — conversation, an agent's
brief, its system prompt. Sans carries the interface around it: labels,
buttons, status, traces. Mono only for literal machine text such as a command
or an id.

That split is the whole typographic idea. Keep it strictly.

### Shape and space

```
--radius:14px   --radius-sm:10px   --pad:18px
--lift: 0 1px 2px rgba(62,48,32,.05), 0 6px 18px rgba(62,48,32,.05)
```

The shadow is warm-brown and barely visible. Never a grey drop shadow.

## Layout

**Header**, one row, full width: the wordmark `AgentForge` in serif; two status
pills showing the runtime and the orchestrator, each with a small state dot;
then, pushed right, the mode tabs (Build / Run / Admin) and a pill holding the
username and a quiet sign-out link. Admin is hidden unless the account has it.

**Main**, below, a two-column grid: `minmax(0,1.6fr) minmax(0,1fr)`. The
conversation is the larger left column, always. It is the point of the product
and must never be squeezed by the panels beside it.

- **Left**: a scrolling conversation, then a composer pinned at the bottom —
  an auto-growing textarea, a microphone button, and a send button.
- **Right**: changes by mode. In Build, the requirement checklist and build
  bar. In Run, the crew strip, live trace, workspace files, and run bar. In
  Admin, the overview.

Below ~900px the grid becomes one column, conversation first.

## The screens

### Sign-in

A centred card on the paper background. Above it stands **the keeper**: a
character drawn in SVG from the same family as the agents — a rounded body,
a visor-like face, no human detail.

The keeper is alive. Its pupils follow the cursor around the page. When focus
enters the password field it covers its eyes, and uncovers them when the field
is revealed by the eye toggle. The password field has that reveal toggle.

Sign-up and sign-in are the same card with a swap link, not two pages. Errors
appear under the fields in `--fault`, in a sentence, never a code.

### Build — the interview

Left: the conversation with the interviewer. Right: a checklist of the seven
things it must learn, each row showing a tick when answered and the answer in
one clipped line. A progress line reads `Answered 3 of 7`. At the bottom,
**Start over** and **Create team**, the latter quiet until the interview is
ready.

### Run — the live team

Right column, top to bottom:

1. **Crew strip** — every agent as a small figure with its name, laid out with
   the supervisor first and its reports beside it, connected by hairlines.
   Several can be lit at once, because agents work in parallel.
2. **Live trace** — a stream of events, each a left-bordered line coloured by
   kind: delegation, thinking, tool call, result, error, blocked, reused.
3. **Workspace** — the only folder the team can reach. File chips with size
   and a remove ×, plus **Add files** and **Add folder**. Dropping files
   anywhere on the conversation outlines the whole pane in accent and dashed
   border with the words *Drop files or a folder to add them*.
4. **Run bar** — team selector, run id, **Download spec**, **Retry what
   failed** (only after a failure), and **Delete team** in outline-red.

Clicking a figure opens that agent's **brief**: its figure at 44px, nickname
and role, then a definition list — Can use, Leads, Thinks, Model, Expects,
Produces — then its system prompt in serif. Below that, editable nickname,
personality and memory, and finally, set apart above a rule, **Delete this
agent** in outline-red.

### Admin

A quiet report, not a dashboard. A row of large plain numbers with small
labels beneath, then a table of teams and runs. No gauges, no sparklines, no
cards with gradient tops.

## The agents' figures

Every agent gets a figure generated deterministically from a 32-bit hash of
its name, so the same agent always looks the same and no two neighbours
collide. Never a photo, never an avatar service, never an emoji.

From the hash:

- **Hue** from `[204, 162, 28, 268, 340, 94, 186, 14]`
- **Leaders** are desaturated and darker (`sat 10%`, `light 32%`) with squarer
  head corners (`rx 3`); **workers** are brighter (`sat 34%`, `light 46%`) with
  round heads (`rx 6`). Rank reads as restraint, not as a badge or a crown.
- **Shoulder width** varies 3–5px, **visor width** 10–15px, and one figure in
  three has a small antenna.

States: `idle` (still), `talking` (active), `done`, `failed`. A working figure
is marked by colour and a subtle cue, not by bouncing.

## Motion

Almost none, deliberately. The only continuous animation in the entire product
is the recording pulse on the microphone — it is a recording light, and it
earns its motion by being the one thing that must not be forgotten.

Everything else is a 150ms colour or border transition on hover and focus.
No entrance animations, no staggered fades, no skeleton shimmer.

Honour `prefers-reduced-motion: reduce` by stopping the pulse.

## Writing

Sentence case everywhere. No ALL-CAPS labels, no tracked-out eyebrows.

Buttons say what will happen: **Create team**, **Retry what failed**, **Add
folder**. Not *Submit*, not *OK*.

Errors explain and give the next step in one sentence: *"Microphone permission
was refused. Allow it in the browser's address bar, then press the button
again."* Never a code, never an apology.

Destructive actions name what goes and say it cannot be undone, and they arm
on the first press rather than opening a modal: the button becomes *Really
delete route_analyst?* and completes on the second press.

Empty states invite the next action: *"Empty. Drop files or a folder here —
agents can only read what you add."*

## Do not

- No cream-and-terracotta palette, no `#D97757`-family accent.
- No card kit: identical rounded boxes with the same grey shadow for every
  piece of content regardless of its importance.
- No numbered step markers (01 / 02 / 03) unless the content is genuinely a
  sequence.
- No gradient washes, no glassmorphism, no neon-on-black.
- No middle-dot meta strings as decoration, no `→` appended to button text.
- No icon fonts or icon libraries; draw the few icons needed as inline SVG.
- No colour as the only carrier of meaning — pair every state colour with a
  word.

## Build it as

One self-contained HTML file: markup, CSS custom properties, and script
together. No framework, no build step, no external requests — the whole point
is that it runs locally. Responsive to mobile, visible keyboard focus rings,
real `aria-label`s on the icon-only buttons, and contrast that passes in both
themes.
