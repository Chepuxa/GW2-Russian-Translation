from pathlib import Path
from contextlib import closing
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "translations/ru/hero/test.json"
        self.file.parent.mkdir(parents=True)
        self.row = {"id": 12, "en": "<c=%str1%>Level %num1%</c>",
                    "ru": "<c=%str1%>Уровень %num1%</c>",
                    "context": "Hero label", "status": "reviewed", "evidence": "fixture",
                    "src_hash": catalog.source_hash("<c=%str1%>Level %num1%</c>")}
        self.write([self.row])

    def write(self, rows):
        self.file.write_text(json.dumps({"format": 1, "language": "ru",
            "section": "hero/test", "strings": rows}, ensure_ascii=False), encoding="utf-8")

    def test_actual_catalog_valid(self):
        self.assertGreaterEqual(len(catalog.load_catalog()), 99)

    def test_source_hash_protocol_reference(self):
        # Known hash recorded from the actual GW2 addon for the English word Hero.
        self.assertEqual(catalog.source_hash("Hero"), "1cf94521bda482b6")

    def test_lost_placeholder_rejected(self):
        self.row["ru"] = "<c=%str1%>Уровень</c>"
        self.write([self.row])
        with self.assertRaisesRegex(ValueError, "tokens changed"):
            catalog.load_catalog(self.root)

    def test_changed_source_rejected(self):
        self.row["en"] += "!"
        self.write([self.row])
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            catalog.load_catalog(self.root)

    def test_duplicate_id_across_files_rejected(self):
        other = self.file.with_name("duplicate.json")
        other.write_bytes(self.file.read_bytes())
        with self.assertRaisesRegex(ValueError, "duplicate ID"):
            catalog.load_catalog(self.root)

    def test_duplicate_json_property_rejected(self):
        self.file.write_text('{"format":1,"format":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON property"):
            catalog.load_catalog(self.root)

    def test_changed_tag_order_rejected(self):
        self.row["ru"] = "</c>Уровень %num1%<c=%str1%>"
        self.write([self.row])
        with self.assertRaisesRegex(ValueError, "tag order"):
            catalog.load_catalog(self.root)

    def test_drafts_excluded_and_build_is_deterministic(self):
        draft = dict(self.row, id=13, status="draft", ru="")
        self.write([self.row, draft])
        rows = catalog.load_catalog(self.root)
        one, two = self.root / "one", self.root / "two"
        m1, m2 = catalog.build(rows, one), catalog.build(rows, two)
        self.assertEqual(m1, m2)
        self.assertEqual((one / "reviewed-ru.json").read_bytes(),
                         (two / "reviewed-ru.json").read_bytes())
        payload = json.loads((one / "reviewed-ru.json").read_text(encoding="utf-8"))
        self.assertEqual(set(payload["strings"]), {"12"})
        self.assertEqual(set(payload["src_hash"]), {"12"})

    def test_merge_preserves_unrelated_and_signed_hash(self):
        path = self.root / "patch.db"
        with closing(sqlite3.connect(path)) as db:
            db.executescript(catalog.SCHEMA)
            db.execute("INSERT INTO patch VALUES(77,0,'Unrelated',123,NULL,4)")
            db.execute("INSERT INTO meta VALUES('version','4')")
            db.commit()
        rows = [self.row, dict(self.row, id=13, status="draft")]
        version = catalog.merge_patch(path, rows)
        self.assertEqual(version, 5)
        with closing(sqlite3.connect(path)) as db:
            saved = dict(db.execute("SELECT key,text FROM patch"))
            self.assertEqual(saved[77], "Unrelated")
            self.assertNotIn(13, saved)
            self.assertEqual(saved[12], self.row["ru"])
            self.assertEqual(db.execute("SELECT kind,src_hash FROM patch WHERE key=12").fetchone(),
                             (0, catalog.signed_hash(self.row["src_hash"])))
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_process_guard_scopes_installation_and_blocks_unknown_paths(self):
        game = self.root / "game"
        records = [
            {"Name":"Gw2-64.exe", "ProcessId":101, "ExecutablePath":str(game / "Gw2-64.exe")},
            {"Name":"Local_Translator_RU.exe", "ProcessId":os.getpid(), "ExecutablePath":str(game / "module.exe")},
            {"Name":"Other.exe", "ProcessId":102, "ExecutablePath":str(self.root / "other/Other.exe")},
        ]
        self.assertEqual(catalog.images_in_game(records, game), {"gw2-64.exe"})
        self.assertEqual(catalog.images_in_game([{"Name":"Gw2-64.exe","ProcessId":103,"ExecutablePath":None}],game), {"gw2-64.exe"})

    def make_game(self):
        game = self.root / "game"
        module = game / "addons/text_translator/modules/local_translator_ru"
        (module / "RU").mkdir(parents=True)
        (game / "Gw2-64.exe").write_bytes(b"fixture; never executed")
        (module / "module.toml").write_text('name="test"',encoding="utf-8")
        return game, module

    def test_packaged_install_initializes_cache_and_preserves_unrelated_patch(self):
        game,module = self.make_game()
        with patch("catalog.running_images", return_value=set()):
            result = catalog.install([self.row], game, False, initialize_cache=True)
            self.assertEqual(result["installed_reviewed"],1)
            with closing(sqlite3.connect(module / "cache.db")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM translations").fetchone()[0],0)
            with closing(sqlite3.connect(module / "RU/patch_ru.db")) as db,db:
                db.execute("INSERT INTO patch VALUES(77,0,'User edit',123,NULL,1)")
            catalog.install([self.row],game,False,initialize_cache=True)
            with closing(sqlite3.connect(module / "RU/patch_ru.db")) as db:
                self.assertEqual(db.execute("SELECT text FROM patch WHERE key=77").fetchone()[0],"User edit")

    def test_running_game_blocks_initialization_before_any_cache_write(self):
        game,module = self.make_game()
        with patch("catalog.running_images",return_value={"gw2-64.exe"}):
            with self.assertRaisesRegex(ValueError,"Close GW2"):
                catalog.install([self.row],game,False,initialize_cache=True)
        self.assertFalse((module / "cache.db").exists())


if __name__ == "__main__":
    unittest.main()
