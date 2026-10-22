from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "tools"))
import catalog
import drafts


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rows=[self.row("Hello %str1%","Привет %str1%",12),
                   self.row("Broken %num1%","Сломано",13),self.row("Untranslated","",14)]
        self.save()
        imported=self.root / "data/imported"
        imported.mkdir(parents=True)
        for name in ("glossary_ru.json","cache_ru.glossary.json"):
            (imported/name).write_text('{}',encoding="utf-8")
        drafts.write_json(imported / "manifest.json",{"files":{p.name:{"sha256":drafts.digest(p)} for p in imported.iterdir()}})
        drafts.write_json(self.root / "translations/ru/test.json",{"format":1,"language":"ru","section":"test","strings":[{"id":99,"en":"Existing","ru":"Уже есть","context":"fixture","status":"reviewed","src_hash":catalog.source_hash("Existing"),"evidence":"fixture"}]})

    def row(self,en,ru,game_id):
        return {"key":catalog.source_hash(en),"en":en,"ru":ru,"ids":[game_id],"status":"draft","context":"","origin":"machine-cache"}

    def save(self):
        buckets={}
        for row in self.rows:buckets.setdefault(row["key"][:2],[]).append(row)
        for key,rows in buckets.items():
            drafts.write_json(self.root / f"drafts/ru/p-{key}.json",{"format":1,"language":"ru","kind":"source-drafts","strings":rows})

    def test_raw_machine_translation_is_kept_but_not_promoted_on_formatting_error(self):
        result=drafts.check(self.root)
        self.assertEqual(result["drafts"],3)
        self.assertEqual(result["formatting_to_review"],1)
        with self.assertRaisesRegex(ValueError,"repair formatting"):
            drafts.propose(self.rows[1]["key"],13,"Hero",self.root/"proposal.json",root=self.root)

    def test_cache_build_contains_all_translations_and_no_reviewed_patch(self):
        output=self.root / "build"
        result=drafts.build_cache(output,self.root)
        self.assertEqual(result["cached_lines"],2)
        self.assertEqual(result["game_cache_entries"],2)
        with closing(sqlite3.connect(output/"cache.db")) as db:
            self.assertEqual(db.execute("SELECT text FROM translations WHERE id=13").fetchone()[0],"Сломано")
            self.assertEqual(db.execute("SELECT src_hash FROM translations WHERE id=12").fetchone()[0],catalog.signed_hash(self.rows[0]["key"]))
        with zipfile.ZipFile(output/"bootstrap-cache.zip") as archive:
            self.assertNotIn("RU/patch_ru.db",archive.namelist())
            records=[json.loads(line) for line in archive.read("RU/cache_ru.jsonl").decode().splitlines()]
            self.assertEqual({r["s"]:r["t"] for r in records},{r["en"]:r["ru"] for r in self.rows if r["ru"]})

    def test_community_edit_is_used_by_next_cache_build(self):
        self.rows[0]["ru"]="Здравствуй %str1%"
        self.save()
        drafts.build_cache(self.root/"new-build",self.root)
        with closing(sqlite3.connect(self.root/"new-build/cache.db")) as db:
            self.assertEqual(db.execute("SELECT text FROM translations WHERE id=12").fetchone()[0],"Здравствуй %str1%")

    def test_promotion_creates_hash_protected_contextual_proposal(self):
        output=self.root/"proposal.json"
        drafts.propose(self.rows[0]["key"],12,"Hero → example",output,root=self.root)
        row=json.loads(output.read_text(encoding="utf-8"))["strings"][0]
        self.assertEqual(row["src_hash"],self.rows[0]["key"])
        self.assertEqual(row["status"],"draft")
        self.assertEqual(row["context"],"Hero → example")

    def test_duplicate_game_ids_in_drafts_are_rejected(self):
        self.rows[1]["ids"]=[12]
        self.save()
        with self.assertRaisesRegex(ValueError,"duplicate game ID"):
            drafts.load(self.root)

    def test_original_snapshot_is_not_silently_changed(self):
        (self.root/"data/imported/glossary_ru.json").write_text('{"changed":true}',encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"snapshot changed"):
            drafts.build_cache(self.root/"bad-build",self.root)

    def test_reviewed_ids_are_never_seeded_with_machine_cache(self):
        path=self.root/"translations/ru/test.json"
        document=json.loads(path.read_text(encoding="utf-8"))
        document["strings"][0]["id"]=12
        drafts.write_json(path,document)
        drafts.build_cache(self.root/"review-protected",self.root)
        with closing(sqlite3.connect(self.root/"review-protected/cache.db")) as db:
            self.assertIsNone(db.execute("SELECT text FROM translations WHERE id=12").fetchone())

    def test_local_protocol_test_ids_are_not_presented_as_game_ids(self):
        self.rows[0]["ids"]=[1000000001]
        self.save()
        with self.assertRaisesRegex(ValueError,"game ID"):
            drafts.load(self.root)


if __name__=="__main__":
    unittest.main()
