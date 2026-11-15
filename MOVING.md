# Moving AgentForge to another laptop

## The short version

```powershell
# on the OLD laptop, inside the project folder
.\package-for-move.ps1          # makes a zip on the Desktop

# carry the zip across, unzip it on the NEW laptop, then
.\setup-on-new-laptop.ps1       # rebuilds the venv and checks the rest
.\run.ps1
```

## What moves, and what must not

| | Moves | Why |
|---|---|---|
| `backend/ web/ tests/` | yes | the code |
| `agentforge.db` | yes | accounts, teams, run history |
| `teams/` | yes | agent folders, their memories, workspace files |
| `.secrets/server.key` | yes | session tokens are signed with it |
| `.env` | yes | models and keys |
| `hermes_catalogue.json` | yes | the closed tool registry |
| `.venv/` | **no** | Windows binaries built against that machine's Python |
| `__pycache__/` | **no** | bytecode for the old build |
| `.multica-outbox/` | **no** | transient staging |

Nothing in the database or in `team.json` holds an absolute path — every path
is derived at runtime from the teams directory, the owner and the team name.
So the project works from any folder on the new machine, under any username.

## What lives outside the project folder

These are not in the zip and have to be dealt with separately.

**Hermes.** Install it on the new laptop and sign in. Then:

```powershell
hermes auth list        # is it signed in to a provider?
hermes profile list     # are your af-* profiles there?
```

Your teams expect `af-<you>`, `af-<you>-w` and `af-<you>-wc`. A missing one is
recreated automatically on first use. But if you had pointed `-w` or `-wc` at a
particular model or provider with `hermes config set`, that setting lives in
Hermes's own folder and needs doing again.

To bring the Hermes side across intact instead, copy `%USERPROFILE%\.hermes\`
as well — it holds the profiles, their configuration, and Hermes's own
credentials. Treat it as sensitive.

**Python.** 3.11 or newer; this was built against 3.13.

**faster-whisper**, if you use voice. The model re-downloads on first use.

## If you skip the server key

A new one is generated on first start. The only effect is that everyone signs
in again — passwords still work, because each hash carries its own salt, and
teams are untouched.

## Security

The zip holds your API keys, your password hashes and the key your sessions
are signed with. Move it on a USB stick or an encrypted transfer, not by
email or a chat app, and delete it from both machines once the new one works.

If the old laptop is being sold or passed on, rotate the keys in `.env`
afterwards and sign out everywhere from the account menu.

## A note on OneDrive

The project currently sits inside OneDrive. If the new laptop signs in to the
same account the folder will sync on its own, which is convenient — but
`teams/`, `.env`, `.secrets/` and `agentforge.db` sync too, so your keys and
password hashes end up in the cloud.

Sync has also silently reverted files mid-edit on this project. For a working
copy, a folder outside OneDrive is the safer home.
