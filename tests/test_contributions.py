from contextlib import closing
import base64
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "components/local-translator"))
import catalog
from contribution_store import Collector, Inbox, KnownIndex, canonical, repository_name, validate_batch
from contribution_github import GitHub, GitHubError
from contribution_ui import Panel, make_server
import contributions


def known_index(path, rows=()):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT);"
            "INSERT INTO meta VALUES('catalog_version','fixture/1');"
            "CREATE TABLE sources(src_hash TEXT PRIMARY KEY);"
            "CREATE TABLE identities(id INTEGER,src_hash TEXT,PRIMARY KEY(id,src_hash));")
        for key, en in rows:
            h = catalog.source_hash(en)
            db.execute("INSERT OR IGNORE INTO sources VALUES(?)", (h,))
            db.execute("INSERT INTO identities VALUES(?,?)", (key, h))
    return path


def wait_until(condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.02)
    raise AssertionError("Background operation timed out")


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = Inbox(self.root / "personal")
        self.addCleanup(self.inbox.close)
        self.known = known_index(self.root / "known.db", [(123, "Inventory")])

    def add(self, key=456, en="Speak with the scout."):
        h = catalog.source_hash(en)
        with self.inbox.db:
            self.inbox.capture(key, h, en, "new-text")
        return key, h

    def selection(self):
        return [{k: r[k] for k in ("id", "src_hash", "revision")} for r in self.inbox.listing()["rows"]]

    def test_discovery_classifies_pair_mapping_and_revision(self):
        index = KnownIndex(self.known)
        self.addCleanup(index.close)
        self.assertIsNone(index.classify(123, catalog.source_hash("Inventory")))
        self.assertEqual(index.classify(999, catalog.source_hash("Inventory")), "new-id")
        self.assertEqual(index.classify(123, catalog.source_hash("Updated inventory")), "changed-source")
        self.assertEqual(index.classify(999, catalog.source_hash("New original")), "new-text")

    def test_opt_in_capture_before_translation_and_restart(self):
        collector = Collector(self.root / "personal", self.known)
        self.addCleanup(collector.close)
        collector.ready.wait(3)
        collector.capture(456, int(catalog.source_hash("Before consent"), 16), "Before consent")
        collector.close()
        self.assertEqual(self.inbox.listing()["total"], 0)
        self.inbox.configure(True, "")
        collector = Collector(self.root / "personal", self.known)
        collector.ready.wait(3)
        collector.capture(456, int(catalog.source_hash("No model reply"), 16), "No model reply")
        collector.capture(123, int(catalog.source_hash("Inventory"), 16), "Inventory")
        collector.capture(457, 0, "Invalid source hash")
        collector.capture(1000000001, int(catalog.source_hash("Test phrase"), 16), "Test phrase")
        collector.close()
        second = Inbox(self.root / "personal")
        self.addCleanup(second.close)
        rows = second.listing()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["en"], "No model reply")
        self.assertEqual(rows[0]["ru"], "")

    def test_dedup_and_distinct_source_revisions(self):
        self.add()
        self.add()
        self.add(en="The scout has moved.")
        rows = self.inbox.listing()["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["sightings"] for r in rows), [1, 2])
        self.assertTrue(all(r["warnings"] for r in rows))

    def test_machine_reply_cannot_replace_human_edit(self):
        key, h = self.add()
        with self.inbox.db:
            self.inbox.translation(key, h, "Плохой машинный перевод")
        row = self.inbox.listing()["rows"][0]
        self.inbox.edit(key, h, row["revision"], "Поговорите с разведчиком.", "Диалог", "pending")
        with self.inbox.db:
            self.inbox.translation(key, h, "Поздний машинный перевод")
        row = self.inbox.listing()["rows"][0]
        self.assertEqual(row["ru"], "Поговорите с разведчиком.")
        self.assertEqual(row["origin"], "human")

    def test_export_is_deterministic_selected_only_and_always_draft(self):
        self.add()
        self.add(key=457, en="Other private local row")
        selection = self.selection()[:1]
        one = self.inbox.prepare(selection, "fixture/1")
        self.assertEqual(one, self.inbox.prepare(selection, "fixture/1"))
        self.assertEqual(len(one["strings"]), 1)
        self.assertEqual(one["strings"][0]["status"], "draft")
        for forbidden in ("first_seen", "last_seen", "sightings", "directory", "author", "repository"):
            self.assertNotIn(forbidden, canonical(one))
        validate_batch(one)

    def test_stale_preview_is_rejected_and_late_edit_stays_pending(self):
        key, h = self.add()
        selection = self.selection()
        payload = self.inbox.prepare(selection, "fixture/1", "owner/project")
        self.inbox.edit(key, h, 0, "Перевод", "Контекст", "pending")
        with self.assertRaisesRegex(ValueError, "изменились"):
            self.inbox.prepare(selection, "fixture/1")
        self.inbox.mark_submitted(payload["batch"], "owner/project", "https://github.com/owner/project/pull/1")
        self.assertEqual(self.inbox.listing()["total"], 1)

    def test_bad_tokens_block_export_and_source_only_is_allowed(self):
        key, h = self.add(en="You have %num1% coins.<br>")
        self.inbox.edit(key, h, 0, "У вас монеты.", "", "pending")
        with self.assertRaisesRegex(ValueError, "подстановки"):
            self.inbox.prepare(self.selection(), "fixture/1")
        self.inbox.edit(key, h, 1, "", "", "pending")
        validate_batch(self.inbox.prepare(self.selection(), "fixture/1"))

    def test_sensitive_text_is_flagged(self):
        self.add(en="My account is Player.1234 at email@example.com")
        self.assertTrue(self.inbox.listing()["rows"][0]["warnings"])

    def test_russian_search_ignores_case(self):
        key, h = self.add()
        self.inbox.edit(key, h, 0, "Поговорите с РАЗВЕДЧИКОМ.", "", "pending")
        self.assertEqual(self.inbox.listing(search="разведчиком")["total"], 1)

    def test_new_catalog_reconciles_existing_queue_without_deleting_history(self):
        self.add(key=123, en="Inventory")
        index = KnownIndex(self.known)
        self.addCleanup(index.close)
        self.inbox.reconcile(index)
        self.assertEqual(self.inbox.listing()["total"], 0)
        self.assertEqual(self.inbox.listing("catalogued")["total"], 1)

    def test_index_includes_accepted_batches_and_source_revisions(self):
        self.add()
        payload = self.inbox.prepare(self.selection(), "fixture/1")
        destination = self.root / "contributions/ru"
        destination.mkdir(parents=True)
        (destination / (payload["batch"]+".json")).write_text(json.dumps(payload), encoding="utf-8")
        (self.root / "components.lock.json").write_text('{"project_version":"fixture"}', encoding="utf-8")
        with patch.object(contributions.drafts, "load", return_value=[{"key": catalog.source_hash("Inventory"), "ids": [123]}]), patch.object(contributions.catalog, "load_catalog", return_value=[]):
            result = contributions.build_index(self.root / "rebuilt.db", self.root)
        self.assertEqual(result["contribution_rows"], 1)
        index = KnownIndex(self.root / "rebuilt.db")
        self.addCleanup(index.close)
        self.assertIsNone(index.classify(456, catalog.source_hash("Speak with the scout.")))

    def test_public_batch_validator_rejects_tampering_and_reviewed(self):
        self.add()
        payload = self.inbox.prepare(self.selection(), "fixture/1")
        for field, value in (("status", "reviewed"), ("src_hash", "0"*16), ("ru", "Different content")):
            broken = copy.deepcopy(payload)
            broken["strings"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_batch(broken)

    def test_repository_validation_prevents_path_and_host_injection(self):
        self.assertEqual(repository_name("https://github.com/owner/project.git"), "owner/project")
        for name in ("owner/project/../../bad", "https://evil.test/a/b", "owner/project?x=y", "owner/..", "-x/y"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                repository_name(name)


class FakeGitHub(GitHub):
    """Stateful GitHub API fake; exercises the same fork/tree/commit/ref/PR orchestration."""
    def __init__(self):
        self.calls, self.branches, self.file, self.pulls = [], {}, None, []
        self.tree_request = None

    def api(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if endpoint == "user": return {"login": "player"}
        if endpoint == "repos/team/project": return {"owner": {"login": "team"}, "default_branch": "main"}
        if "/contents/tools/contributions.py" in endpoint: return {"type": "file"}
        if endpoint == "repos/team/project/git/ref/heads/main": return {"object": {"sha": "base"}}
        if endpoint == "repos/team/project/forks":
            return {"fork": True, "full_name": "player/project", "owner": {"login": "player"}, "parent": {"full_name": "team/project"}}
        if endpoint.endswith("/git/commits/base"): return {"tree": {"sha": "base-tree"}}
        if "/git/ref/heads/community" in endpoint:
            if endpoint not in self.branches: raise GitHubError("Not found", 404)
            return {"object": {"sha": "new-commit"}}
        if endpoint.endswith("/git/trees"):
            self.tree_request = data
            self.file = data["tree"][0]["content"]
            return {"sha": "new-tree"}
        if endpoint.endswith("/git/commits"): return {"sha": "new-commit"}
        if endpoint.endswith("/git/refs"):
            self.branches["repos/player/project/git/ref/heads/" + data["ref"][11:].replace("/", "%2F")] = True
            return {}
        if "/contents/contributions/ru/" in endpoint:
            return {"encoding": "base64", "content": base64.b64encode(self.file.encode()).decode()}
        if "/pulls?" in endpoint: return self.pulls
        if endpoint.endswith("/pulls") and method == "POST":
            self.pulls.append({"html_url": "https://github.com/team/project/pull/1"})
            return self.pulls[0]
        raise AssertionError((method, endpoint, data))


class SubmissionTests(InboxTests):
    # Inherit the inbox fixtures, but run only the tests defined here (see load_tests below).
    def test_fork_submission_writes_one_file_and_retry_reuses_pr(self):
        self.add()
        payload = self.inbox.prepare(self.selection(), "fixture/1")
        github = FakeGitHub()
        one = github.submit(payload, "team/project")
        two = github.submit(payload, "team/project")
        self.assertEqual(one, two)
        self.assertEqual(len(github.pulls), 1)
        self.assertEqual(github.tree_request["base_tree"], "base-tree")
        self.assertEqual(len(github.tree_request["tree"]), 1)
        self.assertTrue(github.tree_request["tree"][0]["path"].startswith("contributions/ru/"))
        self.assertFalse(any(method in {"PATCH", "PUT", "DELETE"} for method, _, _ in github.calls))

    def test_existing_branch_with_different_data_is_never_overwritten(self):
        self.add()
        payload = self.inbox.prepare(self.selection(), "fixture/1")
        github = FakeGitHub()
        github.submit(payload, "team/project")
        github.file = "unrelated content"
        count = len(github.calls)
        with self.assertRaisesRegex(GitHubError, "другой пакет"):
            github.submit(payload, "team/project")
        self.assertFalse(any(method in {"PATCH", "PUT", "DELETE"} for method, _, _ in github.calls[count:]))

    def test_network_failure_keeps_pending_snapshot_for_retry(self):
        self.add()
        self.inbox.configure(True, "team/project")
        github = FakeGitHub()
        panel = Panel(self.root, self.known, self.root / "personal", github)
        self.addCleanup(panel.inbox.close)
        with patch.object(github, "submit", side_effect=GitHubError("Offline")):
            panel.submit(self.selection(), "team/project", True)
            wait_until(lambda: panel.job["state"] == "failed")
        self.assertEqual(self.inbox.listing()["total"], 1)
        panel.submit(self.selection(), "team/project", True)
        wait_until(lambda: panel.job["state"] == "complete")
        self.assertEqual(self.inbox.listing("submitted")["total"], 1)

    def test_no_publication_without_consent(self):
        self.add()
        panel = Panel(self.root, self.known, self.root / "personal", FakeGitHub())
        self.addCleanup(panel.inbox.close)
        with self.assertRaisesRegex(ValueError, "Подтвердите"):
            panel.submit(self.selection(), "team/project", False)
        self.assertEqual(panel.github.calls, [])


class PanelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.panel = Panel(root, known_index(root / "known.db"))
        self.addCleanup(self.panel.inbox.close)
        self.server = make_server(self.panel)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        result = response.status, content
        connection.close()
        return result

    def test_browser_requests_need_token_and_reject_cross_origin(self):
        self.assertEqual(self.request("GET", "/api/rows")[0], 403)
        headers = {"Authorization": "Bearer " + self.panel.token}
        self.assertEqual(self.request("GET", "/api/rows", headers=headers)[0], 200)
        headers["Origin"] = "https://evil.test"
        self.assertEqual(self.request("GET", "/api/rows", headers=headers)[0], 403)
        del headers["Origin"]
        headers["Host"] = "rebinding.test:"+str(self.server.server_port)
        self.assertEqual(self.request("GET", "/api/status", headers=headers)[0], 403)

    def test_read_does_not_enable_collection_and_json_post_is_required(self):
        code, html = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Мастерская перевода", html.decode())
        self.assertNotIn(self.panel.token, html.decode())
        self.assertFalse(self.panel.inbox.settings()["enabled"])
        headers = {"Authorization": "Bearer "+self.panel.token}
        body = {"enabled": True, "repository": ""}
        self.assertEqual(self.request("POST", "/api/settings", body, headers)[0], 400)
        headers["Content-Type"] = "application/json"
        self.assertEqual(self.request("POST", "/api/settings", body, headers)[0], 200)
        self.assertTrue(self.panel.inbox.settings()["enabled"])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(InboxTests))
    suite.addTests(loader.loadTestsFromTestCase(PanelHTTPTests))
    suite.addTests(SubmissionTests(name) for name in SubmissionTests.__dict__ if name.startswith("test_"))
    return suite


if __name__ == "__main__":
    unittest.main()
