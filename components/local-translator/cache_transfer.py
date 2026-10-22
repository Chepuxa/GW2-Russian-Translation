"""Merge a locally exported cache without replacing existing translations."""
from pathlib import Path
from contextlib import closing
import datetime
import json
import msvcrt
import os
import shutil
import sqlite3
import uuid
import zipfile

from precache_tool import running_game_or_module, write_json


def import_cache(game: Path, archive: Path, progress_file: Path | None = None) -> dict:
    module = game.resolve() / "addons/text_translator/modules/local_translator_ru"
    data, work = module / "RU", module / "precache"
    work.mkdir(parents=True, exist_ok=True)
    progress = progress_file or work / "import-result.json"
    lock = (work / "run.lock").open("a+b")
    lock.seek(0)
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        if running_game_or_module(game):
            raise RuntimeError("Close Guild Wars 2 before importing a cache.")
        extracted = work / "imports" / uuid.uuid4().hex
        extracted.mkdir(parents=True)
        allowed = {"cache.db", "RU/cache_ru.jsonl", "RU/cache_ru.glossary.json",
                   "RU/glossary_ru.json", "RU/map_ru.db", "RU/patch_ru.db"}
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                name = entry.filename.replace("\\", "/")
                if name not in allowed:
                    raise ValueError(f"Unexpected cache entry: {name}")
                destination = extracted / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.read(entry))
        incoming_glossary = extracted / "RU/glossary_ru.json"
        if not incoming_glossary.is_file() or (
            json.loads(incoming_glossary.read_text(encoding="utf-8-sig")) !=
            json.loads((data / "glossary_ru.json").read_text(encoding="utf-8-sig"))
        ):
            raise ValueError("The cache uses a different Russian glossary. Keep the same glossary on both PCs.")
        incoming_snapshot = extracted / "RU/cache_ru.glossary.json"
        current_snapshot = data / "cache_ru.glossary.json"
        if current_snapshot.exists() and incoming_snapshot.exists() and (
            json.loads(incoming_snapshot.read_text(encoding="utf-8")) !=
            json.loads(current_snapshot.read_text(encoding="utf-8"))
        ):
            raise ValueError("The cache uses different markup-protection rules; run Prepare-Cache instead.")

        merged = {}
        for path in (extracted / "RU/cache_ru.jsonl", data / "cache_ru.jsonl"):
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        row = json.loads(line)
                        if not isinstance(row.get("s"), str) or not isinstance(row.get("t"), str):
                            raise ValueError("Invalid translation cache record")
                        merged[row["s"]] = row["t"]
        incoming_rows = []
        if (extracted / "cache.db").exists():
            with closing(sqlite3.connect((extracted / "cache.db").as_uri()+"?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Imported SQLite cache is damaged")
                incoming_rows = db.execute("SELECT id,cache_key,src_hash,version,timestamp,text FROM translations WHERE cache_key='ru' AND version=1").fetchall()

        backup = work / "backups" / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-import")
        backup.mkdir(parents=True, exist_ok=True)
        for name in ("cache_ru.jsonl", "cache_ru.glossary.json"):
            if (data / name).exists():
                shutil.copy2(data / name, backup / name)
        db_path = module / "cache.db"
        if db_path.exists():
            with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(backup / "cache.db")) as dest:
                source.backup(dest)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS translations (
                id INTEGER NOT NULL,cache_key TEXT NOT NULL,src_hash INTEGER NOT NULL,
                version INTEGER NOT NULL,timestamp INTEGER NOT NULL,text TEXT NOT NULL,
                PRIMARY KEY(cache_key,id)) WITHOUT ROWID""")
            db.executemany("INSERT OR IGNORE INTO translations VALUES (?,?,?,?,?,?)", incoming_rows)
            added = db.total_changes
        reviewed_added = 0
        incoming_patch = extracted / "RU/patch_ru.db"
        if incoming_patch.exists():
            import catalog
            with closing(sqlite3.connect(incoming_patch.as_uri()+"?mode=ro", uri=True)) as source:
                if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Imported reviewed database is damaged")
                accepted = source.execute("SELECT key,text,src_hash FROM patch WHERE kind=0 AND src_hash IS NOT NULL").fetchall()
            patch = data / "patch_ru.db"
            if patch.exists():
                with closing(sqlite3.connect(patch)) as source, closing(sqlite3.connect(backup / "patch_ru.db")) as dest:
                    source.backup(dest)
            with closing(sqlite3.connect(patch)) as db:
                db.executescript(catalog.SCHEMA)
                with db:
                    version = int(dict(db.execute("SELECT k,v FROM meta")).get("version") or 0) + 1
                    before = db.total_changes
                    db.executemany("INSERT OR IGNORE INTO patch(key,kind,text,src_hash,version) VALUES(?,0,?,?,?)", [(key,text,h,version) for key,text,h in accepted])
                    reviewed_added = db.total_changes - before
                    db.execute("INSERT OR REPLACE INTO meta VALUES('version',?)", (str(version),))
        target = data / "cache_ru.jsonl"
        temporary = target.with_suffix(".import.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for source, translation in merged.items():
                stream.write(json.dumps({"s":source,"t":translation},ensure_ascii=False)+"\n")
        os.replace(temporary, target)
        if incoming_snapshot.exists() and not current_snapshot.exists():
            shutil.copy2(incoming_snapshot, current_snapshot)
        if (extracted / "RU/map_ru.db").exists() and not (data / "map_ru.db").exists():
            shutil.copy2(extracted / "RU/map_ru.db", data / "map_ru.db")
        result = {"phase":"complete", "local_cache_entries":len(merged),
                  "game_rows_added":added, "reviewed_rows_added":reviewed_added, "backup":str(backup)}
        write_json(progress,result)
        return result
    except Exception as error:
        write_json(progress,{"phase":"failed","error":str(error)})
        raise
    finally:
        lock.close()
