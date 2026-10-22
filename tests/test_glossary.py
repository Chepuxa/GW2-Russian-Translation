import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "components/local-translator")]
import glossary
import test_editor
from contribution_editor import Editor, encode, validate_changes


class GlossaryTests(unittest.TestCase):
    setUp = test_editor.EditorTests.setUp

    def seed(self):
        self.path = self.root / "glossary/ru.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.document = {"format": 1, "language": "ru", "note": "Редакторские договорённости",
                         "terms": [{"en": "Power", "ru": "Сила", "context": "Характеристика"}]}
        self.path.write_bytes(encode(self.document))
        return self.editor.glossary_listing()

    def test_actual_glossary_is_valid(self):
        self.assertGreaterEqual(len(glossary.load()["terms"]), 31)

    def test_add_edit_search_and_restart_preserve_context_and_note(self):
        data = self.seed()
        row = data["rows"][0]
        self.editor.save_term(row["identity"], data["revision"], "Power", "Сила", "Базовая характеристика персонажа")
        self.editor.close()
        self.editor = Editor(self.root, self.personal, self.inbox)
        data = self.editor.glossary_listing(search="ПЕРСОНАЖА")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["rows"][0]["ru"], "Сила")
        self.assertEqual(self.editor.glossary_listing(search="сила")["total"], 1)
        self.assertEqual(glossary.load(self.root)["note"], self.document["note"])
        self.assertEqual(self.editor.glossary_listing(offset=30)["rows"], [])

    def test_new_dictionary_can_be_created_when_missing(self):
        data = self.editor.glossary_listing()
        self.assertIsNone(data["revision"])
        self.editor.save_term(None, None, " Trait ", " Черта ", "")
        self.assertEqual(glossary.load(self.root)["terms"], [{"en": "Trait", "ru": "Черта", "context": ""}])

    def test_duplicates_rejected_but_different_meanings_allowed(self):
        data = self.seed()
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.editor.save_term(None, data["revision"], " power ", "Мощь", "характеристика ")
        self.editor.save_term(None, data["revision"], "Power", "Мощь", "Название умения")
        self.assertEqual(self.editor.glossary_listing()["total"], 2)

    def test_stale_save_and_delete_cannot_overwrite_external_changes(self):
        data = self.seed()
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.editor.save_term(data["rows"][0]["identity"], data["revision"], "Power", "Мощь", "Характеристика")
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.editor.delete_term(data["rows"][0]["identity"], data["revision"])
        self.assertEqual(glossary.load(self.root)["terms"][0]["ru"], "Сила")

    def test_delete_can_be_undone_and_changes_remain_exportable(self):
        data = self.seed()
        result = self.editor.delete_term(data["rows"][0]["identity"], data["revision"])
        self.assertEqual(self.editor.glossary_listing()["total"], 0)
        self.editor.save_term(None, result["revision"], **result["term"])
        self.assertEqual(self.editor.glossary_listing()["total"], 1)
        data = self.editor.glossary_listing()
        self.editor.save_term(data["rows"][0]["identity"], data["revision"], "Power", "Сила", "Уточнённое значение")
        changes = self.editor.changes()
        self.assertEqual([c["path"] for c in changes], ["glossary/ru.json"])
        document = self.editor.prepare([{k: changes[0][k] for k in ("path", "revision")}])
        validate_changes(document)
        github = test_editor.CatalogGitHub({"glossary/ru.json": encode(self.document)})
        self.assertIn("/pull/", github.submit_changes(document, "team/project"))
        self.assertEqual(set(github.remote), {"glossary/ru.json"})

    def test_invalid_terms_and_duplicate_rename_leave_original_untouched(self):
        data = self.seed()
        before = self.path.read_bytes()
        for en, ru, context in [("", "Сила", ""), ("Power", "  ", ""), ("Power", "Сила\0", ""), ("x" * 2001, "Сила", "")]:
            with self.subTest(en=en[:10]), self.assertRaises(ValueError):
                self.editor.save_term(None, data["revision"], en, ru, context)
        self.assertEqual(self.path.read_bytes(), before)
        broken = copy.deepcopy(self.document)
        broken["terms"][0]["reviewed"] = True
        with self.assertRaises(ValueError):
            glossary.validate(broken)

    def test_api_search_save_and_delete_require_local_authentication(self):
        import http.client
        import threading
        from contribution_ui import Panel, make_server
        from test_contributions import known_index
        self.seed()
        panel = Panel(self.root, known_index(self.root / "known.db"), self.personal, project=self.root)
        self.addCleanup(panel.inbox.close)
        self.addCleanup(panel.editor.close)
        server = make_server(panel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, token=True):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + panel.token
            connection.request(method, "/api/editor/glossary" + path, json.dumps(body) if body else None, headers)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        self.assertEqual(request("GET", "", token=False)[0], 403)
        code, listing = request("GET", "?search=Power")
        self.assertEqual(code, 200)
        body = {"identity": listing["rows"][0]["identity"], "revision": listing["revision"], "en": "Power", "ru": "Сила", "context": "Статистика героя"}
        self.assertEqual(request("POST", "/save", body, token=False)[0], 403)
        self.assertEqual(request("POST", "/save", body)[0], 200)
        _, listing = request("GET", "")
        body = {"identity": listing["rows"][0]["identity"], "revision": listing["revision"]}
        self.assertEqual(request("POST", "/delete", body)[0], 200)
        self.assertEqual(request("GET", "")[1]["count"], 0)
