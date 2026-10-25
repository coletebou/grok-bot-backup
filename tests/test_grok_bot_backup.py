import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import grok_bot_backup as gbb  # noqa: E402

ALPHA = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "aaaaaaaa-0000-4000-8000-000000000002"
AGENTS = {"agents": [
    {"id": "1001", "agentId": ALPHA, "legacyAgentId": "aaaaaaaa-0000-4000-8000-0000000000ff", "name": "Alpha",
     "title": "Ops", "kind": "GROK_BOT_AGENT_KIND_AGENT", "createdAtMs": "1", "updatedAtMs": "99",
     "viewerSessionId": "session-1", "viewerIsOwner": True},
    {"id": "1002", "agentId": ROOM, "name": "Team Room", "kind": "GROK_BOT_AGENT_KIND_ROOM", "memberAgentIds": [ALPHA]},
]}
ROUTINE = {"id": "daily-digest", "name": "Daily digest", "prompt": "Summarize the day. Key: crsr_ABCDEFGHIJKLMNOP",
           "schedule": "CRON_TZ=UTC 7 9 * * 1-5", "isEnabled": True, "trigger": {"type": "cron"},
           "runs": [{"status": "ok"}], "lastRunAt": "2026-10-24T09:07:00Z", "nextRunAt": "2026-10-27T09:07:00Z"}
AUTOMATIONS = {"agents": [
    {"agentId": ALPHA, "automations": [{"automationId": "auto-1", "recordJson": json.dumps(ROUTINE)}]},
    {"agentId": ROOM},
]}
BOX = {"gatewayUrl": "https://pod-1340.box.example/", "gatewayToken": "gateway-token", "networkToken": "network-token",
       "execDaemonUrl": "https://exec.box.example", "execDaemonAuthToken": "exec-token"}
BOX_AGENTS = [
    {"id": ALPHA, "name": "Alpha", "path": "/home/box/agent-data/agents/a1", "isGroup": False, "memberIds": [],
     "lastMessagePreview": "something private", "unreadCount": 3},
    {"id": ROOM, "name": "Team Room", "isGroup": True, "memberIds": [ALPHA]},
]
SKILLS = [
    {"id": "standup", "name": "Daily standup", "description": "Write the standup", "body": "1. Read the board.",
     "source": "workflow", "runs": [], "lastRunAt": None, "helperScripts": []},
    {"id": "plugin-skill", "name": "Plugin skill", "source": "plugin", "pluginId": "linear", "body": "LONG PLUGIN BODY"},
]


class FakeApi:
    """Stands in for http_post: answers each endpoint from canned data and records every call."""

    def __init__(self, fail_on=None, **overrides):
        self.calls = []
        self.fail_on = fail_on
        self.responses = {"ListGrokBotAgents": AGENTS, "ListGrokBotAccountAutomations": AUTOMATIONS,
                          "EnsureSandBox": BOX, "listAgents": BOX_AGENTS, "getAgentWorkflows": SKILLS, **overrides}

    def __call__(self, url, body, headers, timeout=90):
        self.calls.append((url, body, headers))
        method = url.rsplit("/", 1)[-1]
        if method == self.fail_on:
            raise gbb.BackupError(f"{method}: HTTP 500")
        return self.responses[method]


def run_backup(out, api=None):
    api = api or FakeApi()
    return gbb.backup(out, "user-token", time_zone="UTC", post=api), api


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "backup"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_the_documented_layout(self):
        summary, _ = run_backup(self.out)
        self.assertEqual(summary, {"bots": 1, "group chats": 1, "routines": 1, "your skills": 1, "catalog": 2, "redactions": 1})
        self.assertEqual(sorted(snapshot(self.out)), [
            ".grok-bot-backup",
            "bots/alpha/bot.json",
            "bots/alpha/routines/daily-digest.json",
            "bots/alpha/routines/daily-digest.md",
            "bots/index.json",
            "bots/team-room/bot.json",
            "skills/catalog.json",
            "skills/daily-standup/SKILL.md",
            "skills/daily-standup/skill.json",
        ])
        index = json.loads((self.out / "bots/index.json").read_text())
        self.assertEqual(index[1]["members"], ["Alpha"])
        self.assertEqual(index[0]["routines"][0]["file"], "bots/alpha/routines/daily-digest.md")

    def test_drops_run_history_and_viewer_fields(self):
        run_backup(self.out)
        routine = json.loads((self.out / "bots/alpha/routines/daily-digest.json").read_text())
        self.assertFalse({"runs", "lastRunAt", "nextRunAt"} & routine.keys())
        bot = json.loads((self.out / "bots/alpha/bot.json").read_text())
        self.assertNotIn("viewerSessionId", bot)
        self.assertNotIn("updatedAtMs", bot)
        self.assertEqual(bot["box"]["path"], "/home/box/agent-data/agents/a1")
        self.assertNotIn("lastMessagePreview", bot["box"])

    def test_redacts_token_shaped_strings(self):
        run_backup(self.out)
        for name in ("daily-digest.md", "daily-digest.json"):
            text = (self.out / "bots/alpha/routines" / name).read_text()
            self.assertIn("[redacted]", text)
            self.assertNotIn("crsr_", text)

    def test_catalog_lists_plugin_skills_without_their_bodies(self):
        run_backup(self.out)
        catalog = (self.out / "skills/catalog.json").read_text()
        self.assertIn("Plugin skill", catalog)
        self.assertNotIn("LONG PLUGIN BODY", catalog)

    def test_skill_without_frontmatter_gets_one(self):
        run_backup(self.out)
        text = (self.out / "skills/daily-standup/SKILL.md").read_text()
        self.assertTrue(text.startswith('---\nname: "Daily standup"\ndescription: "Write the standup"\n---\n'))

    def test_calls_only_read_endpoints_with_the_right_credentials(self):
        _, api = run_backup(self.out)
        methods = [url.rsplit("/", 1)[-1] for url, _, _ in api.calls]
        self.assertEqual(methods, ["ListGrokBotAgents", "ListGrokBotAccountAutomations", "EnsureSandBox",
                                   "listAgents", "getAgentWorkflows"])
        self.assertFalse(any("exec" in url for url, _, _ in api.calls))
        backend, box = api.calls[0][2], api.calls[-1][2]
        self.assertEqual(backend["authorization"], "Bearer user-token")
        self.assertEqual(backend["x-cursor-client-type"], "sand")
        self.assertEqual(box["authorization"], "Bearer gateway-token")
        self.assertEqual(box["x-anyrun-network-token"], "network-token")
        self.assertTrue(api.calls[-1][0].startswith("https://pod-1340.box.example/api/"))
        self.assertEqual(api.calls[1][1], {"agentIds": [ALPHA, ROOM], "timeZone": "UTC"})

    def test_rerun_without_changes_is_byte_identical(self):
        run_backup(self.out)
        first = snapshot(self.out)
        run_backup(self.out)
        self.assertEqual(first, snapshot(self.out))

    def test_deleted_routine_disappears_on_the_next_run(self):
        run_backup(self.out)
        run_backup(self.out, FakeApi(ListGrokBotAccountAutomations={"agents": []}))
        self.assertFalse((self.out / "bots/alpha/routines").exists())

    def test_refuses_a_folder_it_did_not_write(self):
        (self.out / "bots").mkdir(parents=True)
        (self.out / "bots/mine.txt").write_text("keep me")
        with self.assertRaisesRegex(gbb.BackupError, "empty --out"):
            run_backup(self.out)
        self.assertEqual((self.out / "bots/mine.txt").read_text(), "keep me")

    def test_failed_fetch_leaves_the_previous_backup_alone(self):
        run_backup(self.out)
        before = snapshot(self.out)
        with self.assertRaises(gbb.BackupError):
            run_backup(self.out, FakeApi(fail_on="getAgentWorkflows"))
        self.assertEqual(before, snapshot(self.out))

    def test_no_bots_is_an_error_not_an_empty_backup(self):
        run_backup(self.out)
        before = snapshot(self.out)
        with self.assertRaisesRegex(gbb.BackupError, "no bots"):
            run_backup(self.out, FakeApi(ListGrokBotAgents={"agents": []}))
        self.assertEqual(before, snapshot(self.out))

    def test_api_key_without_a_gateway_explains_itself(self):
        with self.assertRaisesRegex(gbb.BackupError, "agent login"):
            run_backup(self.out, FakeApi(EnsureSandBox={}))

    def test_unreadable_routine_leaves_the_previous_backup_alone(self):
        run_backup(self.out)
        before = snapshot(self.out)
        for record in ("not json", '"a string"', "[1, 2]"):
            bad = {"agents": [{"agentId": ALPHA, "automations": [{"automationId": "auto-9", "recordJson": record}]}]}
            with self.assertRaisesRegex(gbb.BackupError, "auto-9 came back unreadable"):
                run_backup(self.out, FakeApi(ListGrokBotAccountAutomations=bad))
            self.assertEqual(before, snapshot(self.out))

    def test_bot_without_an_agent_id_is_an_error(self):
        broken = {"agents": [{"id": "1003", "name": "Ghost", "kind": "GROK_BOT_AGENT_KIND_AGENT"}]}
        with self.assertRaisesRegex(gbb.BackupError, "without an agentId"):
            run_backup(self.out, FakeApi(ListGrokBotAgents=broken))
        self.assertFalse(self.out.exists())

    def test_routines_with_the_same_name_never_overwrite_each_other(self):
        records = [dict(ROUTINE, id=rid, name="Check inbox") for rid in ("check-inbox", "check-inbox-2", "check-inbox-3")]
        same = {"agents": [{"agentId": ALPHA, "automations": [
            {"automationId": r["id"], "recordJson": json.dumps(r)} for r in records]}]}
        summary, _ = run_backup(self.out, FakeApi(ListGrokBotAccountAutomations=same))
        self.assertEqual(summary["routines"], 3)
        files = sorted(p.name for p in (self.out / "bots/alpha/routines").glob("*.json"))
        self.assertEqual(len(files), 3)
        index = json.loads((self.out / "bots/index.json").read_text())
        self.assertEqual(len({r["file"] for r in index[0]["routines"]}), 3)

    def test_bad_box_listings_leave_the_previous_backup_alone(self):
        run_backup(self.out)
        before = snapshot(self.out)
        for listing in ([], {"agents": []}, "nope", [{"id": ROOM, "isGroup": True, "memberIds": [ALPHA]}], ["x"]):
            with self.assertRaises(gbb.BackupError):
                run_backup(self.out, FakeApi(listAgents=listing))
            self.assertEqual(before, snapshot(self.out))

    def test_bad_routine_collections_leave_the_previous_backup_alone(self):
        run_backup(self.out)
        before = snapshot(self.out)
        for autos in ({"agents": {}}, {"agents": ["x"]}, {"agents": [{"agentId": ALPHA, "automations": {}}]},
                      {"agents": [{"agentId": ALPHA, "automations": ["x"]}]}):
            with self.assertRaisesRegex(gbb.BackupError, "unreadable"):
                run_backup(self.out, FakeApi(ListGrokBotAccountAutomations=autos))
            self.assertEqual(before, snapshot(self.out))

    def test_non_object_responses_are_errors(self):
        for method in ("ListGrokBotAgents", "ListGrokBotAccountAutomations", "EnsureSandBox"):
            with self.assertRaisesRegex(gbb.BackupError, f"{method} came back unreadable"):
                run_backup(self.out, FakeApi(**{method: ["not", "an", "object"]}))
        self.assertFalse(self.out.exists())

    def test_a_failed_write_leaves_the_previous_backup_alone(self):
        run_backup(self.out)
        before = snapshot(self.out)
        with mock.patch.object(gbb.Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(gbb.BackupError, "could not write"):
                run_backup(self.out)
        self.assertEqual(before, snapshot(self.out))

    def test_a_failed_swap_restores_the_previous_backup(self):
        run_backup(self.out)
        before = snapshot(self.out)
        real_replace, calls = os.replace, []

        def flaky(src, dst):  # the first move of the new skills/ into place fails; bots/ is already swapped by then
            calls.append(Path(dst).name)
            if calls.count("skills") == 1 and Path(dst).name == "skills" and Path(src).parent.name == "new":
                raise OSError("device busy")
            return real_replace(src, dst)

        with mock.patch.object(gbb.os, "replace", side_effect=flaky):
            with self.assertRaisesRegex(gbb.BackupError, "previous backup was restored"):
                run_backup(self.out)
        self.assertEqual(before, snapshot(self.out))
        self.assertEqual(calls[:4], ["old-bots", "old-skills", "bots", "skills"])

    def test_skill_library_is_read_through_the_same_bot_every_run(self):
        two = [dict(BOX_AGENTS[0], id="zzz"), dict(BOX_AGENTS[0], id="aaa", name="Other")]
        for listing in (two, list(reversed(two))):
            _, api = run_backup(self.out, FakeApi(listAgents=listing))
            self.assertEqual(api.calls[-1][1], {"id": "aaa"})

    def test_bad_skill_responses_leave_the_previous_backup_alone(self):
        run_backup(self.out)
        before = snapshot(self.out)
        bodies = ({"not": "text"}, [], False, 0)
        for skills in ({"workflows": SKILLS}, ["x"], *[[dict(SKILLS[0], body=b)] for b in bodies]):
            with self.assertRaisesRegex(gbb.BackupError, "unreadable"):
                run_backup(self.out, FakeApi(getAgentWorkflows=skills))
            self.assertEqual(before, snapshot(self.out))

    def test_response_order_does_not_change_the_backup(self):
        records = [dict(ROUTINE, id=rid, name="Check inbox", prompt=rid) for rid in ("bbb-1", "aaa-1")]
        autos = [{"automationId": r["id"], "recordJson": json.dumps(r)} for r in records]
        skills = [dict(SKILLS[0], id=sid, body=sid) for sid in ("s-2", "s-1")]

        def api(order):
            return FakeApi(ListGrokBotAccountAutomations={"agents": [{"agentId": ALPHA, "automations": order(autos)}]},
                           getAgentWorkflows=order(skills))

        run_backup(self.out, api(list))
        first = snapshot(self.out)
        run_backup(self.out, api(lambda xs: list(reversed(xs))))
        self.assertEqual(first, snapshot(self.out))

    def test_token_shaped_keys_are_redacted_without_losing_entries(self):
        record = dict(ROUTINE, trigger={"type": "webhook", "crsr_ABCDEFGHIJKLMNOP": 1, "crsr_QRSTUVWXYZABCDEF": 2})
        autos = {"agents": [{"agentId": ALPHA, "automations": [{"automationId": "auto-1", "recordJson": json.dumps(record)}]}]}
        run_backup(self.out, FakeApi(ListGrokBotAccountAutomations=autos))
        trigger = json.loads((self.out / "bots/alpha/routines/daily-digest.json").read_text())["trigger"]
        self.assertEqual(trigger, {"type": "webhook", "[redacted]": 1, "[redacted] 2": 2})
        first = snapshot(self.out)
        flipped = dict(ROUTINE, trigger={"crsr_QRSTUVWXYZABCDEF": 2, "crsr_ABCDEFGHIJKLMNOP": 1, "type": "webhook"})
        autos = {"agents": [{"agentId": ALPHA, "automations": [{"automationId": "auto-1", "recordJson": json.dumps(flipped)}]}]}
        run_backup(self.out, FakeApi(ListGrokBotAccountAutomations=autos))
        self.assertEqual(first, snapshot(self.out))

    def test_group_member_without_a_name_does_not_break_the_index(self):
        agents = {"agents": [dict(AGENTS["agents"][0], name=None), AGENTS["agents"][1]]}
        run_backup(self.out, FakeApi(ListGrokBotAgents=agents))
        index = json.loads((self.out / "bots/index.json").read_text())
        self.assertEqual([i["members"] for i in index if i["kind"].endswith("ROOM")], [[ALPHA]])

    def test_token_shaped_names_never_reach_paths(self):
        agents = {"agents": [dict(AGENTS["agents"][0], name="crsr_ABCDEFGHIJKLMNOP")]}
        skills = [dict(SKILLS[0], name="ghp_" + "b" * 36)]
        run_backup(self.out, FakeApi(ListGrokBotAgents=agents, getAgentWorkflows=skills))
        paths = list(snapshot(self.out))
        self.assertFalse([p for p in paths if "crsr" in p.lower() or "ghp" in p.lower()], paths)
        self.assertIn("bots/redacted/bot.json", paths)
        self.assertIn("skills/redacted/SKILL.md", paths)


class HelperTests(unittest.TestCase):
    def test_plain_http_is_refused(self):
        with self.assertRaisesRegex(gbb.BackupError, "non-HTTPS"):
            gbb.http_post("http://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotAgents", {}, {})

    def test_a_token_split_across_lines_is_rejected_without_echoing_it(self):
        for token in ("eyJpart-one\nsecret-part-two", "has space-secret", "tab\tsecret"):
            with self.assertRaises(gbb.BackupError) as caught:
                gbb.find_token(env={"CURSOR_ACCESS_TOKEN": token}, home="/nonexistent", platform="linux")
            self.assertNotIn("secret", str(caught.exception))

    def test_a_bad_header_value_never_reaches_the_error_text(self):
        url = "https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotAgents"
        with self.assertRaises(gbb.BackupError) as caught:
            gbb.http_post(url, {}, {"authorization": "Bearer abc\r\nsecretvalue"})
        self.assertNotIn("secretvalue", str(caught.exception))
        self.assertIn("malformed", str(caught.exception))

    def test_main_never_prints_a_malformed_token(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CURSOR_ACCESS_TOKEN": "line-one\nsecret-line-two"}), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.assertEqual(gbb.main(["--out", "unused"]), 1)
        self.assertNotIn("secret", stderr.getvalue() + stdout.getvalue())

    def test_timeouts_become_backup_errors(self):
        with mock.patch.object(gbb.OPENER, "open", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(gbb.BackupError, "ListGrokBotAgents: timed out"):
                gbb.http_post("https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotAgents", {}, {})

    def test_redirects_are_refused(self):
        self.assertIsNone(gbb.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/"))

    def test_token_from_env_wins(self):
        self.assertEqual(gbb.find_token(env={"CURSOR_ACCESS_TOKEN": " env-token "}, home="/nonexistent", platform="linux"),
                         "env-token")

    def test_token_from_the_cli_login_file(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home, ".config/cursor/auth.json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"accessToken": "file-token"}))
            self.assertEqual(gbb.find_token(env={}, home=home, platform="linux"), "file-token")

    def test_missing_login_says_how_to_fix_it(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaisesRegex(gbb.BackupError, "agent login"):
                gbb.find_token(env={}, home=home, platform="linux")

    def test_slug_and_unique(self):
        self.assertEqual(gbb.slug("Phone number opt-out scrub", "x"), "phone-number-opt-out-scrub")
        self.assertEqual(gbb.slug("!!!", "fallback"), "fallback")
        used = set()
        self.assertEqual(gbb.unique("drover", used, "1"), "drover")
        self.assertEqual(gbb.unique("drover", used, "1"), "drover-1")
        self.assertEqual(gbb.unique("drover", used, "1"), "drover-1-2")
        self.assertEqual(gbb.unique("drover-1", {"drover-1"}, "x"), "drover-1-x")

    def test_redact_patterns(self):
        hits = [0]
        value = {"a": ["Bearer abcdefghijklmnopqrstuvwxyz0123", "ghp_" + "a" * 36, "plain text"]}
        self.assertEqual(gbb.redact(value, hits), {"a": ["[redacted]", "[redacted]", "plain text"]})
        self.assertEqual(hits[0], 2)

    def test_main_prints_errors_without_a_traceback(self):
        stderr = io.StringIO()
        with mock.patch.object(gbb, "find_token", side_effect=gbb.BackupError("no Cursor login found")), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(gbb.main(["--out", "unused"]), 1)
        self.assertEqual(stderr.getvalue(), "grok-bot-backup: no Cursor login found\n")


if __name__ == "__main__":
    unittest.main()
