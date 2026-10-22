"""Validate and compile contextual GW2 translations; install only reviewed entries."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import xxhash

ROOT = Path(__file__).resolve().parents[1]
TOKENS = re.compile(r"%[A-Za-z]+\d*%|<[^>]*>|\[[^\]\r\n]*\]")
TAGS = re.compile(r"<[^>]*>")
CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SCHEMA = """
CREATE TABLE IF NOT EXISTS patch (
 key INTEGER PRIMARY KEY NOT NULL, kind INTEGER NOT NULL, text TEXT,
 src_hash INTEGER, piece INTEGER, version INTEGER);
CREATE INDEX IF NOT EXISTS patch_piece ON patch(piece);
CREATE TABLE IF NOT EXISTS pieces (
 name TEXT PRIMARY KEY NOT NULL, sha256 TEXT NOT NULL, entries INTEGER, size INTEGER);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY NOT NULL, v TEXT);
CREATE TABLE IF NOT EXISTS applied (key INTEGER PRIMARY KEY NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skip (key INTEGER PRIMARY KEY NOT NULL);
CREATE TABLE IF NOT EXISTS dropped (key INTEGER PRIMARY KEY NOT NULL);
"""


def source_hash(en: str) -> str:
    return xxhash.xxh3_64_hexdigest(en.encode("utf-8"))


def signed_hash(value: str) -> int:
    n = int(value, 16)
    return n - (1 << 64) if n >= 1 << 63 else n


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def load_catalog(root: Path = ROOT, overlays: dict | None = None) -> list[dict]:
    rows, ids = [], {}
    overlays = overlays or {}
    files = sorted(set((root / "translations" / "ru").rglob("*.json")) |
                   {root / name for name in overlays})
    if not files:
        raise ValueError("No translation files found")
    for path in files:
        label = path.relative_to(root).as_posix()
        data = overlays[label] if label in overlays else json.loads(
            path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
        if data.get("format") != 1 or data.get("language") != "ru":
            raise ValueError(f"{label}: expected format=1, language=ru")
        if not isinstance(data.get("section"), str) or not data["section"].strip():
            raise ValueError(f"{label}: missing section")
        if not isinstance(data.get("strings"), list):
            raise ValueError(f"{label}: strings must be an array")
        for row in data["strings"]:
            if not isinstance(row, dict):
                raise ValueError(f"{label}: entry must be an object")
            key = row.get("id")
            where = f"{label}: ID {key}"
            if type(key) is not int or not 0 < key < (1 << 32):
                raise ValueError(f"{where}: ID must be a positive 32-bit integer")
            if key in ids:
                raise ValueError(f"{where}: duplicate ID, already in {ids[key]}")
            ids[key] = label
            for field in ("en", "context", "evidence"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise ValueError(f"{where}: missing {field}")
            if row.get("status") not in ("draft", "reviewed"):
                raise ValueError(f"{where}: invalid status")
            if not isinstance(row.get("ru"), str):
                raise ValueError(f"{where}: ru must be text")
            if row["status"] == "reviewed" and not row["ru"].strip():
                raise ValueError(f"{where}: reviewed translation is empty")
            if row.get("src_hash") != source_hash(row["en"]):
                raise ValueError(f"{where}: English source hash mismatch")
            if CONTROLS.search(row["en"] + row["ru"]):
                raise ValueError(f"{where}: unsupported control character")
            if len(row["ru"].encode("utf-8")) > 65535:
                raise ValueError(f"{where}: translation exceeds protocol limit")
            if row["ru"]:
                if Counter(TOKENS.findall(row["en"])) != Counter(TOKENS.findall(row["ru"])):
                    raise ValueError(f"{where}: placeholders or formatting tokens changed")
                if TAGS.findall(row["en"]) != TAGS.findall(row["ru"]):
                    raise ValueError(f"{where}: formatting tag order changed")
            rows.append(dict(row, section=data["section"], file=label))
    return sorted(rows, key=lambda r: r["id"])


def reviewed(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] == "reviewed"]


def merge_patch(path: Path, rows: list[dict]) -> int:
    """Merge accepted entries only. Caller owns backup and process preflight."""
    with closing(sqlite3.connect(path, timeout=20)) as db:
        db.executescript(SCHEMA)
        meta = dict(db.execute("SELECT k,v FROM meta"))
        version = int(meta.get("version") or 0) + 1
        with db:
            db.executemany("""INSERT INTO patch(key,kind,text,src_hash,piece,version)
                VALUES(?,0,?,?,NULL,?) ON CONFLICT(key) DO UPDATE SET
                kind=0,text=excluded.text,src_hash=excluded.src_hash,
                piece=NULL,version=excluded.version""",
                [(r["id"], r["ru"], signed_hash(r["src_hash"]), version)
                 for r in reviewed(rows)])
            db.executemany("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", [
                ("version", str(version)), ("source", "community-ru-local"), ("migrated", "1")])
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Patch database integrity check failed")
    return version


def build(rows: list[dict], output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    accepted = reviewed(rows)
    # Explicit hash-aware installer below is required: some upstream single-file
    # import paths discard src_hash. Never copy this JSON into the live RU folder.
    payload = {"format": 1, "strings": {str(r["id"]): r["ru"] for r in accepted},
               "src_hash": {str(r["id"]): r["src_hash"] for r in accepted}}
    patch_file = output / "reviewed-ru.json"
    patch_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"format": 1, "language": "ru", "reviewed": len(accepted),
                "draft": len(rows) - len(accepted), "source_files": sorted({r["file"] for r in rows}),
                "sha256": hashlib.sha256(patch_file.read_bytes()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def running_images(game: Path) -> set[str]:
    if sys.platform != "win32":
        raise ValueError("Game installation is supported only on Windows")
    command = ("$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=New-Object Text.UTF8Encoding $false; "
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='Gw2-64.exe' OR Name='Local_Translator_RU.exe'\" | "
        "Select-Object Name,ProcessId,ExecutablePath | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    records = json.loads(result.stdout.decode("utf-8-sig", "replace").strip() or "[]")
    return images_in_game([records] if isinstance(records, dict) else records, game)


def images_in_game(records: list[dict], game: Path) -> set[str]:
    """Unknown process paths block installation; another game folder does not."""
    active = set()
    for row in records:
        if row.get("ProcessId") == os.getpid():
            continue  # The packaged translator also acts as the reviewed installer.
        path = row.get("ExecutablePath")
        if not path or Path(path).resolve().is_relative_to(game.resolve()):
            active.add(row["Name"].lower())
    return active


def install(rows: list[dict], game: Path, module_stopped: bool,
            initialize_cache: bool = False) -> dict:
    game = game.resolve(strict=True)
    module = game / "addons/text_translator/modules/local_translator_ru"
    if not (game / "Gw2-64.exe").is_file() or not (module / "module.toml").is_file():
        raise ValueError("Expected an existing GW2 installation with the Russian local module")
    images = running_images(game)
    if "local_translator_ru.exe" in images:
        raise ValueError("Stop the Russian module in Nexus before installing translations")
    if "gw2-64.exe" in images and not module_stopped:
        raise ValueError("Close GW2 or use --module-stopped after stopping the module in Nexus")
    cache = module / "cache.db"
    if not cache.is_file():
        if not initialize_cache:
            raise ValueError("Existing cache.db not found")
        with closing(sqlite3.connect(cache)) as db:
            db.execute("""CREATE TABLE translations (
                id INTEGER NOT NULL,cache_key TEXT NOT NULL,src_hash INTEGER NOT NULL,
                version INTEGER NOT NULL,timestamp INTEGER NOT NULL,text TEXT NOT NULL,
                PRIMARY KEY(cache_key,id)) WITHOUT ROWID""")
            db.commit()
    with closing(sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True)) as db:
        hashes = dict(db.execute("SELECT id,src_hash FROM translations WHERE cache_key='ru'"))
    mismatches = [r["id"] for r in reviewed(rows)
                  if hashes.get(r["id"]) not in (None, 0, signed_hash(r["src_hash"]))]
    if mismatches:
        raise ValueError(f"Game source differs for these IDs; review them first: {mismatches}")
    patch = module / "RU/patch_ru.db"
    backup = game / "addons/text_translator/ru_setup/backups" / (
        "reviewed-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ"))
    backup.mkdir(parents=True, exist_ok=False)
    for name, path in (("patch_ru.db", patch), ("cache.db", cache)):
        if path.is_file():
            with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(backup / name)) as dst:
                src.backup(dst)
    version = merge_patch(patch, rows)
    # Leave cache.db untouched. The module's startup apply() pushes changes to
    # the addon's in-memory cache and persists them through its supported sender.
    report = {"installed_reviewed": len(reviewed(rows)), "patch_version": version,
              "backup": str(backup), "next_step": "Start the Russian module in Nexus or start GW2"}
    (backup / "install.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "build", "install"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--game", type=Path)
    parser.add_argument("--module-stopped", action="store_true")
    args = parser.parse_args()
    try:
        rows = load_catalog(args.root)
        if args.command == "check":
            result = {"valid": True, "total": len(rows), "reviewed": len(reviewed(rows)),
                      "draft": len(rows) - len(reviewed(rows))}
        elif args.command == "build":
            result = build(rows, args.out or args.root / "dist")
        else:
            if args.game is None:
                raise ValueError("--game is required")
            result = install(rows, args.game, args.module_stopped)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
