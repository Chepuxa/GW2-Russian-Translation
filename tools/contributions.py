"""Validate community discovery batches and build the offline known-string index."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components/local-translator"))
import catalog
import drafts
from contribution_store import validate_batch


def load(root=ROOT):
    rows, batches = [], set()
    for path in sorted((root / "contributions/ru").glob("*.json")):
        document = validate_batch(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=catalog.unique_object))
        if path.stem != document["batch"] or document["batch"] in batches:
            raise ValueError(f"Invalid/duplicate contribution filename: {path.name}")
        batches.add(document["batch"])
        rows.extend(document["strings"])
    return rows


def build_index(output: Path, root=ROOT):
    if output.exists():
        raise ValueError("Choose a new known-string index path")
    source_rows = drafts.load(root)
    reviewed = catalog.load_catalog(root)
    submitted = load(root)
    lock = json.loads((root / "components.lock.json").read_text(encoding="utf-8"))
    sources = {r["key"] for r in source_rows}
    pairs = {(key, r["key"]) for r in source_rows for key in r["ids"]}
    for row in reviewed + submitted:
        sources.add(row["src_hash"])
        pairs.add((row["id"], row["src_hash"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(output)) as db, db:
        db.executescript("""CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE sources(src_hash TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE identities(id INTEGER NOT NULL,src_hash TEXT NOT NULL,PRIMARY KEY(id,src_hash)) WITHOUT ROWID;""")
        db.executemany("INSERT INTO sources VALUES(?)", [(h,) for h in sorted(sources)])
        db.executemany("INSERT INTO identities VALUES(?,?)", sorted(pairs))
        digest = hashlib.sha256("\n".join(sorted(sources)).encode()+str(sorted(pairs)).encode()).hexdigest()
        db.executemany("INSERT INTO meta VALUES(?,?)", [("catalog_version", lock["project_version"]+"/"+digest[:12]), ("format", "1")])
    return {"sources": len(sources), "identities": len(pairs), "contribution_rows": len(submitted)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "index"))
    parser.add_argument("--out", type=Path, default=ROOT / "dist/known-strings.db")
    args = parser.parse_args()
    try:
        result = {"valid": True, "contribution_rows": len(load())} if args.command == "check" else build_index(args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
