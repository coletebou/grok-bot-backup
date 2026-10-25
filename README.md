# grok-bot-backup

Back up your Grok Bot to git: every bot, every routine (prompt, trigger, schedule) and the
skills you made, as plain files with clean diffs. One read-only command.

Grok Bot keeps all of this on Cursor's servers, and there's no way to download a private
copy. If a routine gets overwritten or a bot gets deleted, there's nothing to roll back to.
This gives you a copy you own.

> **Unofficial.** Not affiliated with or endorsed by Cursor or xAI. It reads your own
> account through the same private API the Grok Bot app uses, so a Grok Bot update can break
> it. Only use it on accounts you own.

## Quick start

You need Python 3.9+ (no other dependencies) and the [Cursor CLI](https://cursor.com/docs/cli),
signed in with the same account as your Grok Bot:

```bash
agent login

pipx install git+https://github.com/coletebou/grok-bot-backup
grok-bot-backup --out my-grok-bot
```

Or, without installing anything:

```bash
curl -fsSLO https://raw.githubusercontent.com/coletebou/grok-bot-backup/master/grok_bot_backup.py
python3 grok_bot_backup.py --out my-grok-bot
```

```
bots 13, group chats 2, routines 25, your skills 3, catalog 331, redactions 0 -> my-grok-bot
```

## What you get

```
my-grok-bot/
  bots/
    index.json                     every bot and group chat: title, members, routines
    <bot>/bot.json                 the bot's record: title, description, voice, visibility
    <bot>/routines/<name>.md       a readable routine: enabled, when, trigger, full prompt
    <bot>/routines/<name>.json     the full routine record
  skills/
    <skill>/SKILL.md               skills you made (Grok Bot shares them across all your bots)
    <skill>/skill.json             that skill's metadata
    catalog.json                   every skill your bots can see, plugin and built-in skills by name
```

Output is deterministic: sorted keys, stable order, no timestamps. Re-running with nothing
changed produces no diff, so your git history only grows when your setup actually changes.

## What it doesn't back up

- **Memory.** Each bot's memory lives on Cursor's servers and only that bot can read it. No
  API returns it. For a copy, ask each bot to write out what it remembers.
- **Conversations, attachments and downloads.**
- **Run history and next/last run times.** They change on every run, so every backup would be a diff.
- **Secrets.** See below.

## Back up every day

Make the folder a git repo once (`git init`), then run this from cron, launchd or a systemd timer:

```bash
cd ~/my-grok-bot &&
grok-bot-backup --out . --scan &&
git add -A &&
{ git diff --cached --quiet || git commit -qm "backup $(date +%F)"; } &&
git push -q
```

`--scan` runs [gitleaks](https://github.com/gitleaks/gitleaks) over the result and stops before
anything is committed if it flags something. Keep the repo private: routine prompts often name
your own repos, inboxes and servers.

## How it works

Every call only reads:

| Call | What it's for |
|---|---|
| `GrokBotService/ListGrokBotAgents` | your bots and group chats |
| `GrokBotService/ListGrokBotAccountAutomations` | every routine on the account, across all bots |
| `GrokBotService/EnsureSandBox` | the address of your bot computer's gateway (the app makes the same call when it opens) |
| gateway `listAgents`, `getAgentWorkflows` | box paths and the skill library |

How it handles your login:

- The token comes from your `agent login` (the macOS Keychain, or `~/.config/cursor/auth.json`
  on Linux) or from `CURSOR_ACCESS_TOKEN`. It is never printed or written anywhere.
- It is sent only over HTTPS to `api2.cursor.sh` and the gateway that call returns. Redirects
  are refused, so it can't be forwarded anywhere else. It can do anything your Cursor account
  can, so if you set `CURSOR_ACCESS_TOKEN` on a server, treat it like a password.
- `EnsureSandBox` also returns credentials for a remote shell on your bot computer. This tool
  never uses them.

What it writes:

- Strings shaped like tokens (Cursor webhook keys, JWTs, bearer tokens, GitHub, Slack, AWS and
  Google keys) are replaced with `[redacted]` before anything is written.
- It fetches everything before it touches the disk, so a failed call leaves your last backup
  alone.
- It only clears `bots/` and `skills/` in a folder it created itself (marked by
  `.grok-bot-backup`).

## Restoring

- **A routine:** paste its `.md` into a chat with the bot and ask it to recreate the routine.
- **A skill:** give any bot the `SKILL.md` and ask it to save it as a skill.

## Made by

[Cole Tebou](https://x.com/ColeTebou). I also build [Badland Line](https://badland.ai/line),
which lets you text your Grok Bot from any phone.

MIT license.
