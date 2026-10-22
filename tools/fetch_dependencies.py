"""Fetch pinned external binaries/model files; verify SHA256 before staging."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import urllib.request
import zipfile

from release import ROOT, MODULE, safe_file, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=ROOT / ".artifacts/runtime")
    args = parser.parse_args()
    lock = json.loads((ROOT / "components.lock.json").read_text(encoding="utf-8"))
    downloads = []
    for component in lock["components"]:
        if "binary" in component:
            filename = Path(component["binary"]).name
            url = component["repository"] + "/releases/download/v" + component["version"] + "/" + filename
            downloads.append((component["binary"], url, component["sha256"]))
    model = next(c for c in lock["components"] if c["name"] == "OPUS-MT EN-RU INT8")
    prefix = (MODULE / "RU/model").as_posix() + "/"
    for entry in lock["runtime_files"]:
        if entry["path"].startswith(prefix):
            name = entry["path"][len(prefix):]
            downloads.append((entry["path"], model["repository"]+"/resolve/"+model["revision"]+"/"+name, entry["sha256"]))
    if not any(name.startswith(prefix) for name, _, _ in downloads):
        raise ValueError("Pinned model file hashes are missing from components.lock.json")
    for relative, url, expected in downloads:
        target = safe_file(args.assets, relative)
        if target.exists() and sha256(target) == expected:
            print("Verified:", relative)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        digest = hashlib.sha256()
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"GW2-Russian-Translation"}), timeout=60) as response, temporary.open("wb") as out:
            while block := response.read(1024 * 1024):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != expected:
            raise ValueError(f"Downloaded file hash differs: {relative}")
        os.replace(temporary, target)
        print("Downloaded and verified:", relative)
    for component in lock["components"]:
        archive = component.get("archive")
        if not archive:
            continue
        targets = archive["files"]
        if all(safe_file(args.assets, e["path"]).is_file() and sha256(safe_file(args.assets, e["path"])) == e["sha256"] for e in targets):
            print("Verified:", component["name"])
            continue
        request = urllib.request.Request(archive["url"], headers={"User-Agent": "GW2-Russian-Translation"})
        import io
        raw = urllib.request.urlopen(request, timeout=60).read()
        if hashlib.sha256(raw).hexdigest() != archive["sha256"]:
            raise ValueError("Archive checksum differs: " + component["name"])
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            for entry in targets:
                data = bundle.read(entry["member"])
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("Extracted file checksum differs: " + entry["path"])
                target = safe_file(args.assets, entry["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".download")
                temporary.write_bytes(data)
                os.replace(temporary, target)
        print("Downloaded and verified:", component["name"])
    print("External dependencies ready. Build the archive reader and Russian module next.")


if __name__ == "__main__":
    main()
