"""Build one offline package from pinned runtime assets and the reviewed catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

import catalog
import drafts
import contributions
import glossary

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path("game/addons/text_translator/modules/local_translator_ru")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest(root: Path = ROOT) -> str:
    files = sorted((root / "components/local-translator").rglob("*.py"))
    files += sorted((root / "components/local-translator").rglob("*.html"))
    files += sorted((root / "components/local-translator").rglob("*.js"))
    files += [root / "tools/catalog.py", root / "tools/glossary.py", root / "components/local-translator/requirements-build.txt"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def safe_file(base: Path, relative: str) -> Path:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in relative or not path.parts:
        raise ValueError(f"Unsafe package path: {relative}")
    resolved = (base / Path(*path.parts)).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path escapes asset directory: {relative}")
    return resolved


def verify_assets(assets: Path, entries: list[dict]) -> None:
    if not entries:
        raise ValueError("No pinned assets; build the module and lock the local runtime first")
    seen = set()
    for entry in entries:
        if entry["path"].casefold() in seen:
            raise ValueError("Duplicate asset path")
        seen.add(entry["path"].casefold())
        path = safe_file(assets, entry["path"])
        if not path.is_file() or path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Runtime asset integrity check failed: {entry['path']}")


def lock_runtime(assets: Path, root: Path = ROOT) -> dict:
    lock_path = root / "components.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for component in lock["components"]:
        if "binary" in component:
            path = safe_file(assets, component["binary"])
            if sha256(path) != component["sha256"]:
                raise ValueError(f"Pinned upstream binary differs: {component['name']}")
        for entry in component.get("archive", {}).get("files", []):
            if sha256(safe_file(assets, entry["path"])) != entry["sha256"]:
                raise ValueError(f"Pinned archive component differs: {component['name']}")
    entries = []
    for path in sorted((assets / "game").rglob("*")):
        if path.is_file():
            relative = path.relative_to(assets).as_posix()
            if any(part in {"precache", "community", "__pycache__"} for part in path.parts) or path.suffix in {".db", ".jsonl", ".log"}:
                raise ValueError(f"Personal/generated game data must not be bundled: {relative}")
            safe_file(assets, relative)
            entries.append({"path":relative, "size":path.stat().st_size, "sha256":sha256(path)})
    lock["runtime_files"] = entries
    lock["module_source_sha256"] = source_digest(root)
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"locked_files":len(entries), "module_source_sha256":lock["module_source_sha256"]}


def build(assets: Path, output: Path, root: Path = ROOT) -> dict:
    rows = catalog.load_catalog(root)
    glossary.load(root)
    lock = json.loads((root / "components.lock.json").read_text(encoding="utf-8"))
    if lock.get("module_source_sha256") != source_digest(root):
        raise ValueError("Module sources changed. Rebuild the module before packaging.")
    verify_assets(assets, lock["runtime_files"])
    profile = json.loads((root / "profiles/default.json").read_text(encoding="utf-8"))
    if profile["format"] != 1 or not 60 <= profile["glyph"]["glyph_size"] <= 109 or not 0 <= profile["glyph"]["glyph_baseline"] <= 100:
        raise ValueError("Invalid font profile")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output already contains files; choose a new --out directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    payload = output / "payload"
    for entry in lock["runtime_files"]:
        dest = safe_file(payload, entry["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_file(assets, entry["path"]), dest)
    font = payload / "game/glyphCore/cyrillic.ttf"
    if sha256(font) != sha256(root / "assets/fonts/AlegreyaSans-Medium.ttf"):
        raise ValueError("Font asset does not match the pinned runtime")
    (payload / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    data = payload / MODULE / "community"
    for path in sorted((root / "translations/ru").rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("$schema", None)
        document["strings"] = [r for r in document["strings"] if r["status"] == "reviewed"]
        dest = data / path.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    module = payload / MODULE
    contribution_index = contributions.build_index(module / "community/known-strings.db", root)
    settings = ('name = "Russian (Local Translator - experimental)"\ncommand = "Local_Translator_RU.exe"\n'
        'args = ["--lang", "ru", "--no-update", "--editor-port", "0", "--threads", "'+str(profile["translator"]["threads"])+'"]\n')
    (module / "module.toml").write_text(settings, encoding="utf-8")
    cache_output = output / "cache-build"
    cache_info = drafts.build_cache(cache_output, root)
    shutil.copy2(cache_output / "bootstrap-cache.zip", payload / "bootstrap-cache.zip")
    shutil.copy2(cache_output / "cache-manifest.json", payload / "cache-manifest.json")
    # The distributable contains the editable project as well as its runtime.
    for name in ("drafts", "data", "translations", "glossary", "docs", "tools",
                 "components", "assets", "profiles", "schema", "tests", ".github", "contributions"):
        shutil.copytree(root / name, output / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "target"))
    for name in ("README.md", "CONTRIBUTING.md", "requirements.txt", "components.lock.json",
                 "Build-Module.ps1", "Build-DatTool.ps1", ".gitignore"):
        shutil.copy2(root / name, output / name)
    for name in ("installer", "licenses"):
        shutil.copytree(root / name, output / name)
    for path in root.glob("*.cmd"):
        shutil.copy2(path, output / path.name)
    shutil.copy2(root / "docs/INSTALL-RU.txt", output / "README.txt")
    shutil.copy2(root / "THIRD-PARTY.md", output / "THIRD-PARTY.md")
    shutil.copy2(root / "LICENSE", output / "LICENSE")
    shutil.copy2(root / "components/glyphcore/LICENSE", output / "licenses/GlyphCore-public-MIT.txt")
    manifest = {"format":1, "project":"GW2-Russian-Translation", "version":lock["project_version"],
                "module":lock["module_version"], "reviewed":len(catalog.reviewed(rows)),
                "cache":cache_info, "contribution_index":contribution_index, "files":[]}
    for path in sorted(payload.rglob("*")):
        if path.is_file():
            manifest["files"].append({"path":str(path.relative_to(payload)).replace("/", "\\"),
                                      "size":path.stat().st_size, "sha256":sha256(path)})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    archive = output.parent / (output.name+".zip")
    if archive.exists():
        raise ValueError(f"Archive already exists: {archive}")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file() and not path.is_relative_to(cache_output):
                bundle.write(path, output.name + "/" + path.relative_to(output).as_posix())
    return {"package":str(output.resolve()), "archive":str(archive.resolve()),
            "files":len(manifest["files"]), "reviewed":manifest["reviewed"],
            "cache":cache_info, "sha256":sha256(archive)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("lock", "check", "build"))
    parser.add_argument("--assets", type=Path, default=ROOT / ".artifacts/runtime")
    parser.add_argument("--out", type=Path, default=ROOT / "dist/GW2-Russian-Translation")
    args = parser.parse_args()
    try:
        if args.command == "lock":
            result = lock_runtime(args.assets)
        elif args.command == "check":
            lock = json.loads((ROOT / "components.lock.json").read_text(encoding="utf-8"))
            verify_assets(args.assets, lock["runtime_files"])
            if lock["module_source_sha256"] != source_digest():
                raise ValueError("Rebuild the changed module sources")
            result = {"valid":True,"runtime_files":len(lock["runtime_files"])}
        else:
            result = build(args.assets, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
