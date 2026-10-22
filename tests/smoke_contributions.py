"""Check discovery with the real frozen EXE, no model, and the local panel API."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import queue
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import xxhash


def wait_until(test, seconds=15):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        result = test()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError("Frozen module operation timed out")


def main():
    module = Path(sys.argv[1]).resolve()
    data = module / "workshop-smoke"
    data.mkdir()
    personal = data / "RU/contributions"
    personal.mkdir(parents=True)
    # Opt in before launching the module, just as the workshop does.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "components/local-translator"))
    from contribution_store import Inbox, validate_batch
    inbox = Inbox(personal)
    inbox.configure(True, "")
    inbox.close()
    executable = str(module / "Local_Translator_RU.exe")
    command = [executable, "--lang", "ru", "--no-update", "--editor-port", "0", "--lang-dir", str(data / "RU"), "--cache-db", str(data / "cache.db")]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    messages = queue.Queue()

    def read():
        while header := process.stdout.read(4):
            size, = struct.unpack("<I", header)
            messages.put(process.stdout.read(size))

    threading.Thread(target=read, daemon=True).start()
    state_file = data / "panel-state.json"
    panel = None
    try:
        hello = messages.get(timeout=15)
        assert struct.unpack_from("<I", hello)[0] == 2
        def send(key, text, bad_hash=False):
            encoded = text.encode("utf-8")
            h = 0 if bad_hash else xxhash.xxh3_64_intdigest(encoded)
            packet = struct.pack("<BIQH", 0, key, h, len(encoded))+encoded
            process.stdin.write(struct.pack("<I", len(packet))+packet)
            process.stdin.flush()

        send(3960000101, "Workshop integration fixture: untranslated dialogue.")
        send(3960000101, "Workshop integration fixture: untranslated dialogue.")
        send(3960000102, "Inventory")
        send(567188, "Corruption Training")  # Already in the reviewed catalog.
        send(3960000103, "Bad hash fixture", True)
        db_path = personal / "inbox.db"
        def captured():
            if not db_path.is_file(): return False
            with closing(sqlite3.connect(db_path)) as db:
                return db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 2
        wait_until(captured)
        # The deliberately absent model must not prevent original collection.
        with closing(sqlite3.connect(db_path)) as db:
            rows = db.execute("SELECT id,en,ru,sightings,kind FROM inbox ORDER BY id").fetchall()
            assert rows[0][2] == "" and rows[0][3] == 2 and rows[0][4] == "new-text", rows
            assert rows[1][4] == "new-id", rows
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        # Launch the actual frozen panel against that same inbox after process restart.
        panel = subprocess.Popen([executable, "--contribute", "--module-dir", str(module),
            "--inbox-dir", str(personal), "--state-file", str(state_file), "--no-browser"],
            creationflags=subprocess.CREATE_NO_WINDOW)
        wait_until(state_file.is_file)
        url = urlsplit(json.loads(state_file.read_text(encoding="utf-8"))["url"])
        def api(method, path, data=None, token=True):
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
            headers = {"Content-Type": "application/json"}
            if token: headers["Authorization"] = "Bearer "+url.fragment
            connection.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        assert api("GET", "/api/rows", token=False)[0] == 403
        code, listing = api("GET", "/api/rows")
        assert code == 200 and listing["total"] == 2
        selected = [{k: listing["rows"][0][k] for k in ("id", "src_hash", "revision")}]
        code, exported = api("POST", "/api/export", {"selected": selected})
        assert code == 200 and len(exported["strings"]) == 1
        validate_batch(exported)
        assert (module / "tools/github/gh.exe").is_file()
        api("POST", "/api/shutdown", {})
        assert panel.wait(timeout=10) == 0
        report = {"passed": True, "collected_without_model": 2, "exported_selected_only": 1,
                  "restart_preserved_queue": True, "unauthenticated_access_blocked": True,
                  "real_github_pr_tested": False, "fixture": str(data)}
        (data / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for child in (process, panel):
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    main()
