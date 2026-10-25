#!/usr/bin/env python3
"""Back up a Grok Bot account (bots, routines, skills) to plain files you can commit to git.

Unofficial and read-only. It calls the private API the Grok Bot app uses (Connect protocol
on api2.cursor.sh), which is undocumented and can change with any release. It never prints
or stores your token.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

__version__ = "0.1.0"

API = "https://api2.cursor.sh"
DEFAULT_CLIENT_VERSION = "0.75.0"
MARKER = ".grok-bot-backup"
STAGE = ".grok-bot-backup-new"
GENERATED = ("bots", "skills")

# Fields kept per bot. Viewer-, session- and activity-specific fields are left out, so a
# backup only changes when the setup does.
AGENT_FIELDS = (
    "id", "agentId", "legacyAgentId", "name", "title", "description", "harness", "kind",
    "role", "visibility", "teamId", "memberAgentIds", "createdAtMs", "voiceId",
    "voiceSpeed", "voiceLanguage", "draftingDisabled", "routinePolicy",
    "accessGrantsOnly", "publishedAt", "avatarShape", "avatarColor",
)
BOX_AGENT_FIELDS = (
    "id", "path", "origin", "isGroup", "memberIds", "isHiddenFromSidebar",
    "isRemovedFromSidebar", "notificationsEnabled", "notifyOnUpdatesEnabled",
    "pushMessageContent", "createdAt",
)
VOLATILE = {"runs", "lastRunAt", "nextRunAt"}
CATALOG_FIELDS = ("id", "name", "source", "pluginId", "sourceRef", "description")

SECRET_RES = [re.compile(p) for p in (
    r"crsr_[A-Za-z0-9_-]{8,}",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}",
    r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{30,}",
)]


TOKEN_RE = re.compile(r"[\x21-\x7e]+")  # one line of visible ASCII: no spaces, newlines or control characters


def check_token(value, what):
    """Reject a malformed token without ever putting its value in the message."""
    if not TOKEN_RE.fullmatch(value):
        raise BackupError(f"{what} is malformed: it must be one line with no spaces")
    return value


class BackupError(Exception):
    """A failure the user can act on. main() prints it and exits 1."""

    exit_code = 1


class ScanFailed(BackupError):
    exit_code = 3


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so a bearer token is never replayed to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http_post(url, body, headers, timeout=90):
    name = url.rsplit("/", 1)[-1]
    if not url.startswith("https://"):
        raise BackupError(f"refusing a non-HTTPS endpoint for {name}")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
    except ValueError:  # http.client rejects bad header values and quotes them; never show that text
        raise BackupError(f"{name}: a request header was malformed") from None
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"")[:300].decode(errors="replace")
        hint = " (login expired? run `agent login` again)" if e.code in (401, 403) else ""
        raise BackupError(f"{name}: HTTP {e.code}{hint}: {detail}") from None
    except urllib.error.URLError as e:
        raise BackupError(f"{name}: {e.reason}") from None
    except (OSError, http.client.HTTPException) as e:  # timeouts, dropped connections, truncated bodies
        raise BackupError(f"{name}: {e}") from None
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise BackupError(f"{name}: the response was not JSON") from None


def find_token(env=None, home=None, platform=None):
    """The Cursor login: CURSOR_ACCESS_TOKEN, else the Cursor CLI's stored `agent login`."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform
    token = env.get("CURSOR_ACCESS_TOKEN", "").strip()
    if token:
        return check_token(token, "CURSOR_ACCESS_TOKEN")
    if platform == "darwin":
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "cursor-access-token", "-a", "cursor-user", "-w"],
            capture_output=True, text=True,
        )
        token = r.stdout.strip()
        if r.returncode == 0 and token:
            return check_token(token, "the Cursor CLI login in the Keychain")
    config = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    for path in (config / "cursor" / "auth.json", home / ".cursor" / "auth.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if token:
            return check_token(token, f"the Cursor CLI login in {path}")
    raise BackupError("no Cursor login found: install the Cursor CLI and run `agent login`, or set CURSOR_ACCESS_TOKEN")


def system_time_zone():
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz:
        return tz
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    return "UTC"


def slug(text, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or fallback


def unique(name, used, suffix):
    candidate, n = name, 1
    while candidate in used:
        candidate = f"{name}-{suffix}" if n == 1 else f"{name}-{suffix}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def redact(value, hits):
    if isinstance(value, str):
        for rx in SECRET_RES:
            value, n = rx.subn("[redacted]", value)
            hits[0] += n
        return value
    if isinstance(value, list):
        return [redact(v, hits) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in sorted(value.items(), key=lambda kv: str(kv[0])):  # sorted, so collision suffixes are stable
            key = redact(k, hits) if isinstance(k, str) else k
            base, n = key, 2
            while key in out:  # two keys that redact to the same text both survive
                key, n = f"{base} {n}", n + 1
            out[key] = redact(v, hits)
        return out
    return value


def json_text(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def line_text(text):
    return text if text.endswith("\n") else text + "\n"


def routine_markdown(r):
    trigger = r.get("trigger") if isinstance(r.get("trigger"), dict) else {}
    lines = [
        f"# {r.get('name') or 'Untitled routine'}",
        "",
        f"- Enabled: {'yes' if r.get('isEnabled') else 'no'}",
        f"- When: {r.get('triggerDescription') or r.get('schedule') or 'unknown'}",
        f"- Trigger type: {trigger.get('type') or 'schedule'}",
    ]
    if r.get("schedule"):
        lines.append(f"- Schedule: `{r['schedule']}`")
    lines += [
        f"- Created: {r.get('createdAt') or 'unknown'}",
        f"- Id: `{r.get('id')}`",
        "",
        "## Prompt",
        "",
        r.get("prompt") or "",
    ]
    return "\n".join(lines)


def skill_markdown(skill):
    body = skill.get("body") or ""
    if body.lstrip().startswith("---"):
        return body
    front = ["---", f"name: {json.dumps(skill.get('name') or skill.get('id'))}"]
    if skill.get("description"):
        front.append(f"description: {json.dumps(skill['description'])}")
    return "\n".join(front + ["---", "", body])


def gateway(box):
    url, token = box.get("gatewayUrl"), box.get("gatewayToken")
    if not url or not token:
        raise BackupError("EnsureSandBox returned no gateway (a Cursor dashboard API key will not work; use `agent login`)")
    headers = {"content-type": "application/json", "authorization": "Bearer " + check_token(str(token), "the gateway token")}
    extra = box.get("gatewayHeaders") if isinstance(box.get("gatewayHeaders"), dict) else {}
    network = box.get("networkToken") or box.get("anyrunNetworkToken")
    if network and not extra:
        extra = {"x-anyrun-network-token": network}
    headers.update({str(k).lower(): str(v) for k, v in extra.items()})
    return url.rstrip("/"), headers


def prepare_output(out):
    """Refuse a folder whose bots/ or skills/ this tool did not write."""
    out.mkdir(parents=True, exist_ok=True)
    foreign = [d for d in GENERATED if (out / d).exists()]
    if foreign and not (out / MARKER).exists():
        raise BackupError(f"{out} already has {', '.join(d + '/' for d in foreign)} that this tool did not write; use an empty --out folder")


def write_backup(out, files):
    """Write the new backup beside the old one, then swap it in; the old one is only deleted once the swap worked."""
    for leftover in out.glob(STAGE + "-*"):  # from a run that died; one still holding an old backup is kept
        if not any(leftover.glob("old-*")):
            shutil.rmtree(leftover, ignore_errors=True)
    stage = Path(tempfile.mkdtemp(prefix=STAGE + "-", dir=out))
    try:
        for rel, text in sorted(files.items()):
            path = stage / "new" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    except OSError as e:
        shutil.rmtree(stage, ignore_errors=True)
        raise BackupError(f"could not write the backup ({e}); the existing backup was left alone") from None
    (out / MARKER).write_text("Written by grok-bot-backup. bots/ and skills/ in this folder are rewritten on every run.\n",
                              encoding="utf-8")
    try:
        for d in GENERATED:  # move the old backup aside, then put the new one in its place
            if (out / d).exists():
                os.replace(out / d, stage / f"old-{d}")
        for d in GENERATED:
            if (stage / "new" / d).exists():
                os.replace(stage / "new" / d, out / d)
    except OSError as e:
        try:
            for d in GENERATED:  # put back every old folder that was moved aside
                if (stage / f"old-{d}").exists():
                    if (out / d).exists():
                        os.replace(out / d, stage / f"failed-{d}")
                    os.replace(stage / f"old-{d}", out / d)
        except OSError as e2:
            raise BackupError(f"could not swap in the new backup ({e}) or put the old one back ({e2}); "
                              f"the previous backup is in {stage}") from None
        shutil.rmtree(stage, ignore_errors=True)
        raise BackupError(f"could not swap in the new backup ({e}); the previous backup was restored") from None
    shutil.rmtree(stage, ignore_errors=True)


def backup(out, token, *, time_zone, client_version=DEFAULT_CLIENT_VERSION, api=API, post=http_post):
    """Fetch everything first, then rewrite the backup in `out`. Returns a summary dict."""
    out = Path(out)
    api = api.rstrip("/")

    def rpc(method, body):
        headers = {
            "content-type": "application/json",
            "connect-protocol-version": "1",
            "authorization": "Bearer " + token,
            "x-cursor-client-type": "sand",
            "x-cursor-client-version": client_version,
            "x-cursor-client-os": sys.platform,
            "x-cursor-client-source": "sand-desktop",
            "x-sand-box-namespace": "prod",
            "x-ghost-mode": "true",
            "x-request-id": str(uuid.uuid4()),
        }
        result = post(f"{api}/aiserver.v1.GrokBotService/{method}", body, headers)
        if not isinstance(result, dict):
            raise BackupError(f"{method} came back unreadable; the existing backup was left alone")
        return result

    def list_of_objects(value, what):
        """A repeated field: missing means empty (proto3 JSON omits empty lists); anything but a list of objects is an error."""
        value = [] if value is None else value
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise BackupError(f"{what} came back unreadable; the existing backup was left alone")
        return value

    agents = list_of_objects(rpc("ListGrokBotAgents", {}).get("agents"), "ListGrokBotAgents")
    if not agents:
        raise BackupError("ListGrokBotAgents returned no bots; the existing backup was left alone")
    if not all(a.get("agentId") for a in agents):
        raise BackupError("ListGrokBotAgents returned a bot without an agentId; the existing backup was left alone")
    groups = list_of_objects(rpc("ListGrokBotAccountAutomations",
                        {"agentIds": [a["agentId"] for a in agents], "timeZone": time_zone}).get("agents"),
                    "ListGrokBotAccountAutomations")
    # Parse every routine now, so an unreadable one stops the run before the old backup is cleared.
    routines_by_agent = {}
    for g in groups:
        parsed = []
        for auto in list_of_objects(g.get("automations"), f"the routines of {g.get('agentId')}"):
            try:
                r = json.loads(auto.get("recordJson") or "{}")
            except ValueError:
                r = None
            if not isinstance(r, dict):
                raise BackupError(f"routine {auto.get('automationId')} came back unreadable; the existing backup was left alone")
            r.setdefault("id", auto.get("automationId"))
            parsed.append(r)
        routines_by_agent[g.get("agentId")] = parsed

    base, gw = gateway(rpc("EnsureSandBox", {}))
    box_agents = post(base + "/api/listAgents", {}, gw)
    if isinstance(box_agents, dict):
        box_agents = box_agents.get("agents")
    if not isinstance(box_agents, list) or not all(isinstance(b, dict) for b in box_agents):
        raise BackupError("the bot computer's listAgents came back unreadable; the existing backup was left alone")
    # The skill library is shared by every bot, so any one bot can list it. The lowest id keeps that bot the same every run.
    candidates = [b for b in box_agents if b.get("id") and not b.get("isGroup") and not b.get("memberIds")]
    if not candidates:
        raise BackupError("the bot computer listed no bots (is it still starting?); the existing backup was left alone")
    probe = min(candidates, key=lambda b: str(b["id"]))
    skills = post(base + "/api/getAgentWorkflows", {"id": probe["id"]}, gw)
    if not isinstance(skills, list) or not all(isinstance(s, dict) for s in skills):
        raise BackupError("getAgentWorkflows came back unreadable; the existing backup was left alone")
    for s in skills:
        if s.get("source") == "workflow" and s.get("body") is not None and not isinstance(s["body"], str):
            raise BackupError(f"skill {s.get('id')} came back unreadable; the existing backup was left alone")

    # Render the whole backup in memory first: anything odd stops the run before the old backup is cleared.
    files = {}
    hits = [0]

    def box_record(agent):
        ids = {agent.get("agentId"), agent.get("legacyAgentId"), agent.get("id")}
        match = [b for b in box_agents if b.get("id") in ids] or [b for b in box_agents if b.get("name") == agent.get("name")]
        return match[0] if len(match) == 1 else None

    def by_name_then_id(item):  # names collide; ids keep the file each one gets stable across runs
        return slug(item.get("name"), ""), str(item.get("id") or "")

    names = {a["agentId"]: a.get("name") for a in agents}
    index, used, routine_count = [], set(), 0
    for agent in sorted(agents, key=lambda a: ((a.get("name") or "").lower(), a["agentId"])):
        short_id = agent["agentId"][:8]
        bot_dir = unique(slug(redact(agent.get("name") or "", [0]), short_id), used, short_id)
        record = {k: agent[k] for k in AGENT_FIELDS if k in agent}
        box = box_record(agent)
        if box:
            record["box"] = {k: box[k] for k in BOX_AGENT_FIELDS if k in box}
        files[f"bots/{bot_dir}/bot.json"] = json_text(redact(record, hits))

        listed, rused = [], set()
        routines = [redact({k: v for k, v in r.items() if k not in VOLATILE}, hits)
                    for r in routines_by_agent.get(agent["agentId"], [])]
        for r in sorted(routines, key=by_name_then_id):
            rid = str(r.get("id") or "routine")[:8]
            rname = unique(slug(r.get("name"), rid), rused, rid)
            files[f"bots/{bot_dir}/routines/{rname}.json"] = json_text(r)
            files[f"bots/{bot_dir}/routines/{rname}.md"] = line_text(routine_markdown(r))
            listed.append({"name": r.get("name"), "enabled": r.get("isEnabled"),
                           "when": r.get("triggerDescription") or r.get("schedule"),
                           "file": f"bots/{bot_dir}/routines/{rname}.md"})
        routine_count += len(listed)
        index.append({
            "name": agent.get("name"), "title": agent.get("title") or None, "kind": agent.get("kind"),
            "agentId": agent["agentId"], "dir": f"bots/{bot_dir}",
            "members": sorted(names.get(m) or m for m in agent.get("memberAgentIds", [])) or None,
            "routines": listed,
        })
    files["bots/index.json"] = json_text(redact(index, hits))

    catalog = sorted(
        ({f: s[f] for f in CATALOG_FIELDS if s.get(f) not in (None, "")} for s in skills),
        key=lambda s: (s.get("source", ""), (s.get("name") or "").lower(), s.get("id", "")),
    )
    files["skills/catalog.json"] = json_text(redact(catalog, hits))
    library = sorted((redact(s, hits) for s in skills if s.get("source") == "workflow"), key=by_name_then_id)
    sused = set()
    for skill in library:
        sid = str(skill.get("id"))[:8]
        sdir = unique(slug(skill.get("name"), sid), sused, sid)
        files[f"skills/{sdir}/SKILL.md"] = line_text(skill_markdown(skill))
        files[f"skills/{sdir}/skill.json"] = json_text({k: v for k, v in skill.items() if k not in VOLATILE | {"body"}})

    prepare_output(out)
    write_backup(out, files)

    bots = sum(1 for a in agents if a.get("kind") == "GROK_BOT_AGENT_KIND_AGENT")
    return {"bots": bots, "group chats": len(agents) - bots, "routines": routine_count,
            "your skills": len(library), "catalog": len(catalog), "redactions": hits[0]}


def scan(out):
    """Run gitleaks over the backup; raise ScanFailed if it flags anything."""
    if not shutil.which("gitleaks"):
        raise BackupError("--scan needs gitleaks on PATH (https://github.com/gitleaks/gitleaks)")
    r = subprocess.run(["gitleaks", "dir", str(out), "--no-banner", "--redact"], capture_output=True, text=True)
    if r.returncode != 0:
        raise ScanFailed(f"gitleaks flagged the backup in {out}; review it before you commit:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="grok-bot-backup", description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=Path("grok-bot-backup"),
                   help="folder to write the backup into (default: ./grok-bot-backup)")
    p.add_argument("--time-zone", help="IANA time zone for routine schedules (default: this machine's)")
    p.add_argument("--client-version", default=os.environ.get("GROK_BOT_CLIENT_VERSION", DEFAULT_CLIENT_VERSION),
                   help="Grok Bot app version to identify as (default: %(default)s)")
    p.add_argument("--scan", action="store_true", help="run gitleaks over the result and fail if it flags anything")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    try:
        summary = backup(args.out, find_token(), time_zone=args.time_zone or system_time_zone(),
                         client_version=args.client_version)
        if args.scan:
            scan(args.out)
    except BackupError as e:
        print(f"grok-bot-backup: {e}", file=sys.stderr)
        return e.exit_code
    print(", ".join(f"{k} {v}" for k, v in summary.items()) + f" -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
