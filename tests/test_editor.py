import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "components/local-translator")]
import catalog
from contribution_editor import Editor, encode, safe_path, validate_changes
from contribution_store import Inbox
from contribution_github import GitHub, GitHubError


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Проект"
        self.file = self.root / "translations/ru/hero/test.json"
        self.file.parent.mkdir(parents=True)
        self.row = {"id": 12, "en": "Hero", "ru": "Герой", "context": "Hero window",
                    "status": "reviewed", "evidence": "fixture", "src_hash": catalog.source_hash("Hero")}
        self.file.write_bytes(encode({"format": 1, "language": "ru", "section": "hero/test", "strings": [self.row]}))
        self.draft = {"key": catalog.source_hash("Speak with the scout."), "en": "Speak with the scout.",
                      "ru": "", "context": "", "ids": [456], "status": "draft", "origin": "corpus"}
        self.draft_file = self.root / f"drafts/ru/p-{self.draft['key'][:2]}.json"
        self.draft_file.parent.mkdir(parents=True)
        self.draft_file.write_bytes(encode({"format": 1, "language": "ru", "kind": "source-drafts", "strings": [self.draft]}))
        self.personal = Path(self.temp.name) / "personal"
        self.inbox = Inbox(self.personal)
        self.addCleanup(self.inbox.close)
        self.editor = Editor(self.root, self.personal, self.inbox)
        self.addCleanup(lambda: self.editor.close())

    def get(self):
        return self.editor.listing()["rows"][0]

    def promote(self, row=None, game_id=456):
        r = row or self.get()
        return self.editor.promote(r["source"], r["identity"], r["revision"], "Поговорите с разведчиком.",
                                   "История → задание", game_id, "translations/ru/story/test.json")

    def test_search_filter_and_paging_use_entire_catalog(self):
        self.assertEqual(self.editor.listing(state="empty")["total"], 1)
        self.assertEqual(self.editor.listing(view="translations", search="ГЕРОЙ")["total"], 1)
        self.assertEqual(self.editor.listing(offset=30)["rows"], [])
        self.assertEqual(self.editor.listing(search="456")["total"], 1)

    def test_human_draft_edit_preserves_original_id_hash_and_survives_restart(self):
        r = self.get()
        self.editor.save(r["source"], r["identity"], r["revision"], "Поговорите с разведчиком.", "Задание")
        self.editor.close()
        self.editor = Editor(self.root, self.personal, self.inbox)
        after = self.get()
        self.assertEqual(after["en"], self.draft["en"])
        self.assertEqual(after["ids"], [456])
        self.assertEqual(after["origin"], "human")
        self.assertEqual(len(self.editor.changes()), 1)

    def test_promotion_keeps_source_and_generates_only_draft_record(self):
        original = self.draft_file.read_bytes()
        self.promote()
        self.assertEqual(self.draft_file.read_bytes(), original)
        record = next(r for r in catalog.load_catalog(self.root) if r["id"] == 456)
        self.assertEqual(record["status"], "draft")
        self.assertEqual(record["src_hash"], self.draft["key"])
        self.assertEqual(len(self.get()["linked"]), 1)
        self.assertEqual(self.editor.listing(state="unlinked")["total"], 0)

    def test_duplicate_and_wrong_id_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "не соответствует"):
            self.promote(game_id=789)
        self.promote()
        with self.assertRaisesRegex(ValueError, "уже находится"):
            self.promote()

    def test_unknown_id_draft_can_be_edited_before_binding(self):
        doc = json.loads(self.draft_file.read_text(encoding="utf-8"))
        doc["strings"][0]["ids"] = []
        self.draft_file.write_bytes(encode(doc))
        r = self.get()
        self.editor.save(r["source"], r["identity"], r["revision"], "Поговорите с разведчиком.", "Задание")
        self.assertEqual(self.get()["ids"], [])
        with self.assertRaises(ValueError):
            self.promote(game_id=0)

    def test_edit_reviewed_record_requires_new_review(self):
        r = self.editor.listing(view="translations")["rows"][0]
        self.editor.save(r["source"], r["identity"], r["revision"], "Окно героя", "Hero header")
        self.assertEqual(catalog.load_catalog(self.root)[0]["status"], "draft")

    def test_external_edits_and_stale_preview_are_not_overwritten(self):
        r = self.get()
        self.editor.save(r["source"], r["identity"], r["revision"], "Разведчик", "Задание")
        with self.assertRaisesRegex(ValueError, "изменилась"):
            self.editor.save(r["source"], r["identity"], r["revision"], "Потерянная правка", "Задание")
        choice = [{k: v for k, v in self.editor.changes()[0].items() if k in {"path", "revision"}}]
        self.draft_file.write_bytes(self.draft_file.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.editor.prepare(choice)

    def test_create_nested_directory_and_file_included_in_snapshot(self):
        self.editor.create("translations/ru/story/chapter", True)
        self.editor.create("translations/ru/story/chapter/new.json")
        changes = self.editor.changes()
        doc = self.editor.prepare([{k: r[k] for k in ("path", "revision")} for r in changes])
        validate_changes(doc)
        self.assertEqual(len(doc["files"]), 2)
        self.assertTrue(all(r["base_sha256"] is None for r in doc["files"]))

    def test_only_selected_files_exported_with_baselines(self):
        r = self.get()
        self.editor.save(r["source"], r["identity"], r["revision"], "Разведчик", "Задание")
        self.promote()
        choices = [{k: r[k] for k in ("path", "revision")} for r in self.editor.changes() if r["path"].startswith("translations/")]
        doc = self.editor.prepare(choices)
        self.assertEqual(len(doc["files"]), 1)
        altered = copy.deepcopy(doc)
        altered["files"][0]["content"] += "\n"
        with self.assertRaises(ValueError):
            validate_changes(altered)

    def test_path_traversal_reserved_names_and_absolute_paths_rejected(self):
        for name in ["../outside.json", "translations/ru/../../x.json", "translations/ru/CON.json",
                     "translations/ru/bad. /x.json", "C:/x.json", "translations/ru/x.txt", "data/imported/x.json"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.editor.create(name)

    def test_local_discovery_is_available_for_translation_and_promotion(self):
        en = "A new discovery"
        with self.inbox.db:
            self.inbox.capture(789, catalog.source_hash(en), en, "new-text")
        row = self.editor.listing(source="local")["rows"][0]
        self.editor.save(row["source"], row["identity"], row["revision"], "Новая находка", "История")
        self.assertEqual(self.inbox.listing()["rows"][0]["ru"], "Новая находка")
        self.assertEqual(self.editor.listing(source="local")["total"], 1)

    def test_formatting_is_checked_before_promotion(self):
        r = self.get()
        with self.assertRaisesRegex(ValueError, "теги"):
            self.editor.promote(r["source"], r["identity"], r["revision"], "<c>Разведчик</c>", "История", 456, "translations/ru/test.json")

    def test_community_batch_can_be_promoted_without_modifying_immutable_file(self):
        en = "A community discovery"
        h = catalog.source_hash(en)
        with self.inbox.db:
            self.inbox.capture(789, h, en, "new-text")
        r = self.inbox.listing()["rows"][0]
        document = self.inbox.prepare([{k: r[k] for k in ("id", "src_hash", "revision")}], "fixture")
        file = self.root / ("contributions/ru/" + document["batch"] + ".json")
        file.parent.mkdir(parents=True)
        file.write_bytes(encode(document))
        before = file.read_bytes()
        row = self.editor.listing(source="contributions")["rows"][0]
        self.editor.promote(row["source"], row["identity"], row["revision"], "Находка сообщества", "Журнал", 789, "translations/ru/community.json")
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(len(self.editor.listing(source="contributions")["rows"][0]["linked"]), 1)

    def test_editor_http_mutation_requires_token_and_submission_consent(self):
        import http.client
        import threading
        from contribution_ui import Panel, make_server
        from test_contributions import known_index
        panel = Panel(self.root, known_index(self.root / "known.db"), self.personal, project=self.root)
        self.addCleanup(panel.inbox.close)
        self.addCleanup(panel.editor.close)
        server = make_server(panel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(token=True, confirmed=False):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + panel.token
            connection.request("POST", "/api/editor/submit", json.dumps({"selected": [], "repository": "team/project", "confirmed": confirmed}), headers)
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status
        self.assertEqual(request(False), 403)
        self.assertEqual(request(True), 400)
        self.assertEqual(panel.job["state"], "idle")

    def test_github_multi_file_submission_retry_and_conflict(self):
        base = self.file.read_bytes()
        r = self.editor.listing(view="translations")["rows"][0]
        self.editor.save(r["source"], r["identity"], r["revision"], "Окно героя", "Hero header")
        self.promote()
        doc = self.editor.prepare([{k: r[k] for k in ("path", "revision")} for r in self.editor.changes()])
        github = CatalogGitHub({"translations/ru/hero/test.json": base})
        url = github.submit_changes(doc, "team/project")
        self.assertEqual(github.submit_changes(doc, "team/project"), url)
        self.assertEqual(len(github.pulls), 1)
        self.assertEqual(len(github.remote), 2)
        other = CatalogGitHub({"translations/ru/hero/test.json": b"changed upstream"})
        with self.assertRaisesRegex(GitHubError, "другая версия"):
            other.submit_changes(doc, "team/project")
        self.assertEqual(other.remote, {})


class CatalogGitHub(GitHub):
    def __init__(self, originals):
        self.originals, self.remote, self.pulls, self.branches = originals, {}, [], set()

    def api(self, method, endpoint, data=None):
        if endpoint == "user": return {"login": "player"}
        if endpoint == "repos/team/project": return {"owner": {"login": "team"}, "default_branch": "main"}
        if "/contents/tools/catalog.py" in endpoint: return {"type": "file"}
        if endpoint.endswith("/git/ref/heads/main"): return {"object": {"sha": "base"}}
        if endpoint.endswith("/forks"): return {"fork": True, "full_name": "player/project", "owner": {"login": "player"}, "parent": {"full_name": "team/project"}}
        if "/git/ref/heads/community" in endpoint:
            if endpoint not in self.branches: raise GitHubError("Not found", 404)
            return {"object": {"sha": "new"}}
        if "/contents/" in endpoint:
            path = unquote(urlsplit(endpoint).path.split("/contents/")[1])
            files = self.originals if endpoint.startswith("repos/team/") else self.remote
            if path not in files: raise GitHubError("Not found", 404)
            value = files[path]
            return {"encoding": "base64", "content": base64.b64encode(value if isinstance(value, bytes) else value.encode()).decode()}
        if endpoint.endswith("/git/commits/base"): return {"tree": {"sha": "base-tree"}}
        if endpoint.endswith("/git/trees"):
            self.remote = {r["path"]: r["content"] for r in data["tree"]}
            return {"sha": "new-tree"}
        if endpoint.endswith("/git/commits"): return {"sha": "new"}
        if endpoint.endswith("/git/refs"):
            self.branches.add("repos/player/project/git/ref/heads/" + data["ref"][11:].replace("/", "%2F"))
            return {}
        if "/pulls?" in endpoint: return self.pulls
        if "/compare/" in endpoint:
            return {"files": [{"filename": path, "status": "modified" if path in self.originals else "added"} for path in self.remote]}
        if endpoint.endswith("/pulls"):
            self.pulls.append({"html_url": "https://github.com/team/project/pull/42"})
            return self.pulls[-1]
        raise AssertionError((method, endpoint, data))
