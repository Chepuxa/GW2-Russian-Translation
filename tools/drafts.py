"""Keep the full imported corpus/cache and editable, source-keyed community drafts."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile

import catalog

ROOT = Path(__file__).resolve().parents[1]
# Created by the earlier protocol smoke test, not observed game string IDs.
NON_GAME_TEST_IDS = {1000000001, 1000000002, 1000000003, 1000000004}
CACHE_SCHEMA = """CREATE TABLE translations (
 id INTEGER NOT NULL,cache_key TEXT NOT NULL,src_hash INTEGER NOT NULL,
 version INTEGER NOT NULL,timestamp INTEGER NOT NULL,text TEXT NOT NULL,
 PRIMARY KEY(cache_key,id)) WITHOUT ROWID"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def import_snapshot(game: Path, corpus: Path, root: Path = ROOT):
    destination = root / "data/imported"
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Imported snapshot already exists; it is never overwritten automatically")
    if (root / "drafts/ru").exists():
        raise ValueError("Drafts already exist; preserve community edits before importing another snapshot")
    module = game / "addons/text_translator/modules/local_translator_ru"
    sources = {
        "corpus-english.csv": corpus,
        "cache_ru.jsonl": module / "precache/cache_ru.jsonl",
        "cache_ru.glossary.json": module / "precache/cache_ru.glossary.json",
        "glossary_ru.json": module / "RU/glossary_ru.json",
        "precache-progress.json": module / "precache/progress.json",
        "seed_ids.csv": game / "glyphCore/seed_ids.csv",
    }
    destination.mkdir(parents=True, exist_ok=True)
    for name, path in sources.items():
        shutil.copy2(path, destination / name)
    csv.field_size_limit(2**31-1)
    with (destination / "corpus-english.csv").open(encoding="utf-8-sig", newline="") as stream:
        originals = {r["english"] for r in csv.DictReader(stream)}
    with (destination / "cache_ru.jsonl").open(encoding="utf-8") as stream:
        cache = {r["s"]: r["t"] for r in map(json.loads, stream)}
    by_id = {}
    with (destination / "seed_ids.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) >= 2 and row[0].isdigit() and 0 < int(row[0]) < 2**32:
                by_id[int(row[0])] = row[1]
    map_path = (module / "RU/map_ru.db").resolve()
    with closing(sqlite3.connect(map_path.as_uri()+"?mode=ro", uri=True)) as db:
        live = db.execute("SELECT key,en FROM texts").fetchall()
    with (destination / "live-map.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "en"])
        writer.writerows(live)
    by_id.update((key, en) for key, en in live if 0 < key < 2**32)
    by_id = {key:en for key,en in by_id.items() if key not in NON_GAME_TEST_IDS}
    by_text = defaultdict(list)
    for key, en in sorted(by_id.items()):
        by_text[en].append(key)
    buckets = defaultdict(list)
    for en in originals | cache.keys():
        key = catalog.source_hash(en)
        buckets[key[:2]].append({"key":key, "en":en, "ru":cache.get(en, ""),
            "status":"draft", "origin":"machine-cache" if en in cache else "corpus",
            "ids":by_text.get(en, []), "context":""})
    for bucket, rows in sorted(buckets.items()):
        write_json(root / f"drafts/ru/p-{bucket}.json", {"format":1,"language":"ru",
            "kind":"source-drafts", "strings":sorted(rows, key=lambda r:r["key"])})
    manifest = {"format":1,"state":"partial-machine-cache", "corpus_originals":len(originals),
                "cached_lines":len(cache), "unique_drafts":len(originals | cache.keys()),
                "untranslated_drafts":len(originals-cache.keys()),
                "files":{path.name:{"bytes":path.stat().st_size,"sha256":digest(path)}
                         for path in sorted(destination.iterdir()) if path.is_file()}}
    write_json(destination / "manifest.json", manifest)
    return manifest


def load(root: Path = ROOT):
    rows, keys, ids = [], set(), {}
    files = sorted((root / "drafts/ru").glob("p-*.json"))
    if not files:
        raise ValueError("Community drafts are missing")
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=catalog.unique_object)
        if data.get("format") != 1 or data.get("language") != "ru" or data.get("kind") != "source-drafts":
            raise ValueError(f"Invalid draft document: {path.name}")
        for row in data["strings"]:
            if not isinstance(row.get("en"), str) or not isinstance(row.get("ru"), str):
                raise ValueError(f"Draft text is invalid: {path.name}")
            key = catalog.source_hash(row["en"])
            if row.get("key") != key or path.stem != "p-"+key[:2]:
                raise ValueError(f"Draft source/key mismatch: {path.name}")
            if key in keys:
                raise ValueError(f"Duplicate draft key: {key}")
            keys.add(key)
            if row.get("status") != "draft" or not isinstance(row.get("context"), str):
                raise ValueError(f"Promote reviewed entries into translations/ru; draft {key} has an invalid status/context")
            if not isinstance(row.get("ids"), list) or len(set(row["ids"])) != len(row["ids"]):
                raise ValueError(f"Invalid ID list: {key}")
            for game_id in row["ids"]:
                if type(game_id) is not int or not 0 < game_id < 2**32 or game_id in ids or game_id in NON_GAME_TEST_IDS:
                    raise ValueError(f"Invalid/duplicate game ID in drafts: {game_id}")
                ids[game_id] = key
            rows.append(dict(row, file=path.relative_to(root).as_posix()))
    return sorted(rows, key=lambda r:r["key"])


def formatting_ok(en, ru):
    return (Counter(catalog.TOKENS.findall(en)) == Counter(catalog.TOKENS.findall(ru))
            and catalog.TAGS.findall(en) == catalog.TAGS.findall(ru)
            and not catalog.CONTROLS.search(en+ru))


def verify_snapshot(root: Path = ROOT):
    imported = root / "data/imported"
    manifest = json.loads((imported / "manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["files"].items():
        if Path(name).name != name or digest(imported / name) != entry["sha256"]:
            raise ValueError(f"Original snapshot changed: {name}; edit drafts/ru instead")
    return manifest


def check(root: Path = ROOT):
    rows = load(root)
    verify_snapshot(root)
    return {"valid":True, "drafts":len(rows),"with_translation":sum(bool(r["ru"]) for r in rows),
            "untranslated":sum(not r["ru"] for r in rows),
            "formatting_to_review":sum(bool(r["ru"]) and not formatting_ok(r["en"],r["ru"]) for r in rows)}


def build_cache(output: Path, root: Path = ROOT):
    rows = load(root)
    verify_snapshot(root)
    output.mkdir(parents=True, exist_ok=True)
    imported = root / "data/imported"
    local = output / "cache_ru.jsonl"
    with local.open("w", encoding="utf-8") as stream:
        for row in rows:
            if row["ru"]:
                stream.write(json.dumps({"s":row["en"],"t":row["ru"]},ensure_ascii=False)+"\n")
    # Cache records remain automatic, never reviewed patch records. Existing cache
    # entries win on import; reviewed patch entries are applied by the module.
    database = output / "cache.db"
    if database.exists():
        raise ValueError("Cache output already exists; choose a fresh build directory")
    cache_rows = []
    reviewed_ids = {r["id"] for r in catalog.reviewed(catalog.load_catalog(root))}
    for row in rows:
        if row["ru"] and len(row["ru"].encode("utf-8")) <= 65535 and not catalog.CONTROLS.search(row["en"]+row["ru"]):
            cache_rows.extend((key,"ru",catalog.signed_hash(row["key"]),1,0,row["ru"])
                              for key in row["ids"] if key not in reviewed_ids)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(CACHE_SCHEMA)
        db.executemany("INSERT INTO translations VALUES(?,?,?,?,?,?)", cache_rows)
    archive = output / "bootstrap-cache.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(database,"cache.db")
        bundle.write(local,"RU/cache_ru.jsonl")
        for name in ("cache_ru.glossary.json","glossary_ru.json"):
            bundle.write(imported / name,"RU/"+name)
    result = {"drafts":len(rows),"cached_lines":sum(bool(r["ru"]) for r in rows),
              "game_cache_entries":len(cache_rows),"untranslated":sum(not r["ru"] for r in rows),
              "archive_sha256":digest(archive)}
    write_json(output / "cache-manifest.json", result)
    return result


def propose(key, game_id, context, output: Path, section="community/review", root: Path = ROOT):
    row = next((r for r in load(root) if r["key"] == key),None)
    if row is None:
        raise ValueError("Draft key not found")
    if not context.strip() or not row["ru"].strip() or not formatting_ok(row["en"],row["ru"]):
        raise ValueError("Add a translation/context and repair formatting tokens before promotion")
    if row["ids"] and game_id not in row["ids"]:
        raise ValueError("Selected game ID does not match this source draft")
    if any(r["id"] == game_id for r in catalog.load_catalog(root)):
        raise ValueError("This ID is already in translations/ru; edit its existing entry")
    if output.exists():
        raise ValueError("Proposal output already exists")
    if type(game_id) is not int or not 0 < game_id < 2**32:
        raise ValueError("Game ID must be a positive 32-bit integer")
    write_json(output,{"format":1,"language":"ru","section":section,"strings":[{
        "id":game_id,"en":row["en"],"ru":row["ru"],"context":context,
        "src_hash":row["key"],"status":"draft","evidence":"Community edit from source draft "+key,
        "verification":"Editor and screen review pending."}]})
    return {"proposal":str(output),"id":game_id,"status":"draft"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("import","check","build","propose"))
    parser.add_argument("--game",type=Path)
    parser.add_argument("--corpus",type=Path)
    parser.add_argument("--out",type=Path)
    parser.add_argument("--key")
    parser.add_argument("--id",type=int)
    parser.add_argument("--context")
    args = parser.parse_args()
    try:
        if args.command == "import":
            if args.game is None or args.corpus is None:
                raise ValueError("--game and --corpus are required")
            result=import_snapshot(args.game,args.corpus)
        elif args.command == "check":result=check()
        elif args.command == "build":
            if args.out is None:raise ValueError("--out is required")
            result=build_cache(args.out)
        else:
            if not all((args.key,args.id,args.context,args.out)):raise ValueError("--key, --id, --context, --out are required")
            result=propose(args.key,args.id,args.context,args.out)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,KeyError,sqlite3.Error) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
