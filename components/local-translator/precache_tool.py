"""Offline Russian cache preparation. Reads Gw2.dat; never modifies the archive."""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import msvcrt
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import lt_server as lt

CREATE_NO_WINDOW = 0x08000000


def running_game_or_module(game: Path | None = None) -> bool:
    if game is not None:
        import catalog
        return bool(catalog.running_images(game))
    output = subprocess.check_output(
        ["tasklist.exe", "/FO", "CSV", "/NH"], creationflags=CREATE_NO_WINDOW)
    for row in csv.reader(output.decode("utf-8", "replace").splitlines()):
        if len(row) >= 2 and row[0].lower() in ("gw2-64.exe", "local_translator_ru.exe"):
            if row[1].isdigit() and int(row[1]) != os.getpid():
                return True
    return False


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def prepare(args) -> dict:
    game = args.game_dir.resolve()
    module = game / "addons/text_translator/modules/local_translator_ru"
    data = module / "RU"
    work = module / "precache"
    work.mkdir(parents=True, exist_ok=True)
    progress_file = args.progress_file or work / "progress.json"
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    status = {"phase": "starting", "started": time.time(), "done": 0, "total": 0}

    def update(**fields):
        status.update(fields, updated=time.time())
        write_json(progress_file, status)

    lock = (work / "run.lock").open("a+b")
    lock.seek(0)
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        update()
        if running_game_or_module(game):
            raise RuntimeError("Close Guild Wars 2 and its Russian translator before precaching.")
        lt.set_language("ru")
        glossary = lt.Glossary()
        if not glossary.load_file(data / "glossary_ru.json"):
            raise RuntimeError("Russian glossary is missing or invalid.")
        logging.basicConfig(filename=work / "precache.log", encoding="utf-8",
                            level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s", force=True)

        corpus = args.corpus or work / "corpus-english.csv"
        if not args.skip_extract:
            tool = module / "tools/gw2-dat-tool.exe"
            if not tool.is_file():
                raise FileNotFoundError(tool)
            update(phase="extracting")
            with (work / "extract.log").open("w", encoding="utf-8") as log:
                subprocess.run([str(tool), "--dat", str(game / "Gw2.dat"),
                                "strs-export-all", "--out", str(corpus)],
                               stdout=log, stderr=log, check=True,
                               creationflags=CREATE_NO_WINDOW)

        csv.field_size_limit(2**31 - 1)
        texts: set[str] = set()
        ids: dict[int, str] = {}
        map_path = data / "map_ru.db"
        if map_path.is_file():
            with sqlite3.connect(map_path.as_uri() + "?mode=ro", uri=True) as db:
                ids.update((key, en) for key, en in db.execute("SELECT key,en FROM texts")
                           if 0 < key < 1_000_000_000 and en)
        collected = game / "glyphCore/seed_ids.csv"
        if collected.is_file():
            with collected.open(encoding="utf-8", newline="") as stream:
                for row in csv.reader(stream):
                    if len(row) >= 2 and row[0].isdigit():
                        key = int(row[0])
                        if 0 < key < 1_000_000_000:
                            ids[key] = row[1]
        texts.update(ids.values())
        if corpus.is_file():
            with corpus.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    text = row.get("english", "")
                    if text:
                        texts.add(text.rstrip("\0"))
        if not texts:
            raise RuntimeError("No English text corpus is available.")

        # A resumable working cache; live cache files are replaced only after completion.
        work_cache = work / "cache_ru.jsonl"
        live_cache = data / "cache_ru.jsonl"
        for name in ("cache_ru.jsonl", "cache_ru.glossary.json"):
            source, destination = data / name, work / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        engine = lt.Engine(None, glossary, work_cache)
        initial = len(engine.cache)
        # Include translations obtained in-game after an earlier interrupted offline run.
        snapshots_match = False
        try:
            snapshots_match = (json.loads((data / "cache_ru.glossary.json").read_text(encoding="utf-8"))
                               == json.loads((work / "cache_ru.glossary.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        if live_cache.is_file() and snapshots_match:
            with live_cache.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        engine.cache.setdefault(row["s"], row["t"])
                    except (ValueError, KeyError):
                        continue
        lines = {line for text in texts for line in text.split("\n")
                 if line not in engine.cache and glossary.lookup(line) is None
                 and lt.Engine._needs_translation(line)}
        ordered = sorted(lines, key=lambda text: (len(text), text))
        if args.max_new:
            ordered = ordered[:args.max_new]
        update(phase="loading_model", total=len(ordered), corpus_texts=len(texts),
               known_ids=len(ids), cached_before=initial)
        if ordered:
            model = args.model_dir or data / "model"
            translator = lt.CT2Translator(model, threads=args.threads)
            lt.Engine.BATCH = 48
            with engine.cv:
                engine.translator = translator
                engine.queue.extend(ordered)
                engine.queued.update(ordered)
                engine.cv.notify_all()
            started = time.monotonic()
            while True:
                with engine.cv:
                    pending = len(engine.queued)
                    done = len(ordered) - pending
                    failed = len(engine.given_up)
                update(phase="translating", done=done, failed_lines=failed,
                       elapsed_seconds=round(time.monotonic() - started, 1))
                if not pending:
                    break
                time.sleep(2)
        if running_game_or_module():
            raise RuntimeError("Translation finished. Close the game and rerun to import the cache.")

        update(phase="importing")
        backup = work / "backups" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True, exist_ok=True)
        database = module / "cache.db"
        if database.is_file():
            with sqlite3.connect(database) as source, sqlite3.connect(backup / "cache.db") as dest:
                source.backup(dest)
        for name in ("cache_ru.jsonl", "cache_ru.glossary.json"):
            if (data / name).is_file():
                shutil.copy2(data / name, backup / name)
        data.mkdir(exist_ok=True)
        temporary = live_cache.with_suffix(".precache.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for source, translation in engine.cache.items():
                stream.write(json.dumps({"s": source, "t": translation},
                                        ensure_ascii=False) + "\n")
        os.replace(temporary, live_cache)
        shutil.copy2(work / "cache_ru.glossary.json", data / "cache_ru.glossary.json")

        def translated(text: str) -> str | None:
            result = []
            for line in text.split("\n"):
                fixed = glossary.lookup(line)
                if fixed is not None:
                    result.append(fixed)
                elif line in engine.cache:
                    result.append(engine.cache[line])
                elif not lt.Engine._needs_translation(line):
                    result.append(line)
                else:
                    return None
            return "\n".join(result)

        rows = []
        now = int(time.time())
        for key, english in ids.items():
            result = translated(english)
            if result is not None and len(result.encode("utf-8")) <= 65535:
                rows.append((key, "ru", lt.hash_int(lt.english_hash(english)), 1, now, result))
        with sqlite3.connect(database, timeout=30) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS translations (
                id INTEGER NOT NULL, cache_key TEXT NOT NULL, src_hash INTEGER NOT NULL,
                version INTEGER NOT NULL, timestamp INTEGER NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY (cache_key,id)) WITHOUT ROWID""")
            before = db.total_changes
            db.executemany("""INSERT INTO translations VALUES (?,?,?,?,?,?)
                ON CONFLICT(cache_key,id) DO UPDATE SET src_hash=excluded.src_hash,
                version=excluded.version,timestamp=excluded.timestamp,text=excluded.text
                WHERE translations.src_hash != excluded.src_hash""", rows)
            changed = db.total_changes - before
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            game_count = db.execute("SELECT COUNT(*) FROM translations WHERE cache_key='ru'").fetchone()[0]
        result = dict(status, phase="complete", done=len(ordered),
                      local_cache_entries=len(engine.cache), game_cache_entries=game_count,
                      game_rows_added_or_updated=changed,
                      failed_lines=len(engine.given_up), backup=str(backup), finished=time.time(),
                      coverage="Available unencrypted archive strings plus locally collected text. New server-only text may still need translation.")
        write_json(progress_file, result)
        return result
    except Exception as error:
        update(phase="failed", error=str(error))
        logging.exception("Precache failed")
        raise
    finally:
        lock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4, choices=range(1, 33))
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--max-new", type=int, default=0)
    parser.add_argument("--import-cache", type=Path)
    args = parser.parse_args(argv)
    if args.import_cache:
        from cache_transfer import import_cache
        import_cache(args.game_dir, args.import_cache, args.progress_file)
    else:
        prepare(args)
    return 0
