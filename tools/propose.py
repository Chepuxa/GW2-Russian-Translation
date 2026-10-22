"""Export explicitly selected game IDs as contextual translation drafts."""
import argparse
import csv
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from catalog import source_hash, signed_hash


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--ids", required=True, help="Comma-separated game string IDs, not API skill IDs")
    ap.add_argument("--section", required=True, help="For example hero/training")
    ap.add_argument("--context", required=True, help="Observed window, speaker, function, etc.")
    ap.add_argument("--out", type=Path, required=True, help="New output JSON; existing files are never overwritten")
    args = ap.parse_args()
    try:
        ids = {int(k.strip()) for k in args.ids.split(",")}
        if not ids or any(not 0 < k < 2**32 for k in ids):
            raise ValueError("Expected positive 32-bit game string IDs")
        if not args.context.strip() or not args.section.strip():
            raise ValueError("Context and section cannot be empty")
        game = args.game.resolve(strict=True)
        module = game / "addons/text_translator/modules/local_translator_ru"
        found, hashes = {}, {}
        map_file = module / "RU/map_ru.db"
        if map_file.is_file():
            with closing(sqlite3.connect(map_file.as_uri() + "?mode=ro", uri=True)) as db:
                # IDs are user-selected; do not export the full player's map.
                for key in sorted(ids):
                    row = db.execute("SELECT en FROM texts WHERE key=?", (key,)).fetchone()
                    if row:
                        found[key] = (row[0], "local-translator-map")
        missing = ids - found.keys()
        seed = game / "glyphCore/seed_ids.csv"
        if missing and seed.is_file():
            with seed.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.reader(stream):
                    if len(row) >= 2 and row[0].isdigit() and int(row[0]) in missing:
                        found.setdefault(int(row[0]), (row[1], "glyphcore-seed-collector"))
        if ids - found.keys():
            raise ValueError(f"Originals not found for IDs: {sorted(ids-found.keys())}")
        cache = module / "cache.db"
        if cache.is_file():
            with closing(sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True)) as db:
                for key in sorted(ids):
                    row = db.execute("SELECT src_hash FROM translations WHERE cache_key='ru' AND id=?", (key,)).fetchone()
                    if row:
                        hashes[key] = row[0]
        rows = []
        for key in sorted(ids):
            en, origin = found[key]
            h = source_hash(en)
            if hashes.get(key) not in (None, 0, signed_hash(h)):
                raise ValueError(f"ID {key}: collected original differs from current game cache")
            rows.append({"id": key, "en": en, "ru": "", "context": args.context,
                         "src_hash": h, "status": "draft", "evidence": origin,
                         "verification": "Context supplied by contributor; not independently verified."})
        output = {"format": 1, "language": "ru", "section": args.section, "strings": rows}
        # Exclusive creation prevents losing somebody else's existing work.
        with args.out.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
        print(f"Exported {len(rows)} selected drafts to {args.out}")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
