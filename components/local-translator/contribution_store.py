"""Local, opt-in discovery inbox. No network operations or changes to game translations."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import logging
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time

import catalog

log = logging.getLogger(__name__)
TEST_IDS = {1000000001, 1000000002, 1000000003, 1000000004}
MAX_BATCH = 500
MAX_TEXT = 65535
SENSITIVE = re.compile(r"https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\w+\.\d{4}\b|\b(?:\d{1,3}\.){3}\d{1,3}\b", re.I)
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('enabled','false');
INSERT OR IGNORE INTO settings VALUES ('repository','');
CREATE TABLE IF NOT EXISTS inbox (
 id INTEGER NOT NULL, src_hash TEXT NOT NULL, en TEXT NOT NULL,
 ru TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'none',
 context TEXT NOT NULL DEFAULT '', game_build TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
 sightings INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
 revision INTEGER NOT NULL DEFAULT 0, pr_url TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(id,src_hash));
CREATE TABLE IF NOT EXISTS submissions (
 batch TEXT NOT NULL, repository TEXT NOT NULL, payload TEXT NOT NULL,
 revisions TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'prepared',
 pr_url TEXT NOT NULL DEFAULT '', PRIMARY KEY(batch,repository));
"""


def canonical(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def repository_name(value: str) -> str:
    value = value.strip()
    prefix = "https://github.com/"
    if value.startswith(prefix):
        value = value[len(prefix):].rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Укажите репозиторий как owner/name или https://github.com/owner/name")
    if value.split("/")[1] in {".", ".."}:
        raise ValueError("Некорректное имя репозитория")
    return value


def check_text(value, label, empty=True):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label}: требуется текст")
    if len(value.encode("utf-8")) > MAX_TEXT or catalog.CONTROLS.search(value):
        raise ValueError(f"{label}: недопустимая длина или управляющие символы")


def formatting_ok(en, ru):
    return not ru or (Counter(catalog.TOKENS.findall(en)) == Counter(catalog.TOKENS.findall(ru))
                      and catalog.TAGS.findall(en) == catalog.TAGS.findall(ru))


def validate_batch(document):
    if not isinstance(document, dict) or set(document) != {"format", "kind", "language", "catalog_version", "batch", "strings"}:
        raise ValueError("Неверный формат пакета предложений")
    if type(document["format"]) is not int or document["format"] != 1 or document["kind"] != "game-contribution" or document["language"] != "ru":
        raise ValueError("Неподдерживаемый пакет предложений")
    check_text(document["catalog_version"], "Версия каталога")
    rows = document["strings"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
        raise ValueError(f"В пакете должно быть от 1 до {MAX_BATCH} строк")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "src_hash", "en", "ru", "status", "origin", "context", "game_build"}:
            raise ValueError("Неверные поля строки")
        key = row["id"]
        if type(key) is not int or not 0 < key < 2**32 or key in TEST_IDS:
            raise ValueError("Недопустимый игровой ID")
        for field in ("en", "ru", "context", "game_build"):
            check_text(row[field], field, empty=field != "en")
        if row["src_hash"] != catalog.source_hash(row["en"]):
            raise ValueError("Хеш оригинала не совпадает")
        identity = key, row["src_hash"]
        if identity in seen:
            raise ValueError("Повтор строки в пакете")
        seen.add(identity)
        if row["status"] != "draft" or row["origin"] not in {"none", "machine", "human"}:
            raise ValueError("Предложения всегда имеют статус draft")
        if not formatting_ok(row["en"], row["ru"]):
            raise ValueError(f"ID {key}: сохраните теги и подстановки оригинала или очистите перевод")
    unsigned = {k: v for k, v in document.items() if k != "batch"}
    digest = hashlib.sha256(canonical(unsigned).encode("utf-8")).hexdigest()[:24]
    if document["batch"] != digest:
        raise ValueError("Контрольная сумма пакета не совпадает")
    return document


class KnownIndex:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
        self.version = self.db.execute("SELECT value FROM meta WHERE name='catalog_version'").fetchone()[0]

    def classify(self, key, src_hash):
        if self.db.execute("SELECT 1 FROM identities WHERE id=? AND src_hash=?", (key, src_hash)).fetchone():
            return None
        if self.db.execute("SELECT 1 FROM identities WHERE id=? LIMIT 1", (key,)).fetchone():
            return "changed-source"
        if self.db.execute("SELECT 1 FROM sources WHERE src_hash=?", (src_hash,)).fetchone():
            return "new-id"
        return "new-text"

    def close(self):
        self.db.close()


class Inbox:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "inbox.db", timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("casefold", 1, lambda text: text.casefold(), deterministic=True)
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            self.db.close()

    def settings(self):
        with self.lock:
            data = dict(self.db.execute("SELECT name,value FROM settings"))
        return {"enabled": data["enabled"] == "true", "repository": data["repository"]}

    def configure(self, enabled, repository):
        if type(enabled) is not bool or not isinstance(repository, str):
            raise ValueError("Неверные настройки")
        repository = repository_name(repository) if repository.strip() else ""
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", [
                ("enabled", "true" if enabled else "false"), ("repository", repository)])
        return self.settings()

    def collector_status(self):
        with self.lock:
            data = dict(self.db.execute("SELECT name,value FROM settings"))
        return {"active": time.time()-int(data.get("collector_seen", "0")) < 15,
                "version": data.get("collector_version", ""), "dropped": int(data.get("collector_dropped", "0"))}

    def capture(self, key, src_hash, en, kind, game_build=""):
        now = int(time.time())
        with self.lock:
            self.db.execute("""INSERT INTO inbox(id,src_hash,en,kind,game_build,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id,src_hash) DO UPDATE SET
                last_seen=excluded.last_seen,sightings=inbox.sightings+1""",
                (key, src_hash, en, kind, game_build, now, now))

    def translation(self, key, src_hash, ru):
        # A late model reply must never replace a player's edit or a submitted snapshot.
        with self.lock:
            self.db.execute("""UPDATE inbox SET ru=?,origin='machine',revision=revision+1
                WHERE id=? AND src_hash=? AND origin!='human' AND state='pending' AND ru!=?""",
                (ru, key, src_hash, ru))

    def listing(self, state="pending", search="", offset=0):
        if state not in {"pending", "ignored", "submitted", "catalogued"}:
            raise ValueError("Неизвестный раздел")
        offset = max(0, min(int(offset), 10000000))
        query = "state=? AND (instr(casefold(en),casefold(?))>0 OR instr(casefold(ru),casefold(?))>0 OR CAST(id AS TEXT)=?)"
        params = state, search[:200], search[:200], search[:200]
        with self.lock:
            counts = dict(self.db.execute("SELECT state,count(*) FROM inbox GROUP BY state"))
            total = self.db.execute("SELECT count(*) FROM inbox WHERE "+query, params).fetchone()[0]
            rows = [dict(r) for r in self.db.execute("SELECT * FROM inbox WHERE "+query+
                " ORDER BY first_seen DESC,id,src_hash LIMIT 50 OFFSET ?", (*params, offset))]
            for row in rows:
                variants = self.db.execute("SELECT count(*) FROM inbox WHERE id=?", (row["id"],)).fetchone()[0]
                row["warnings"] = ([] if formatting_ok(row["en"], row["ru"]) else ["Повреждены теги или подстановки"])
                if SENSITIVE.search(row["en"]):
                    row["warnings"].append("Возможны личные данные, адрес или имя аккаунта")
                if variants > 1:
                    row["warnings"].append("У этого ID несколько оригиналов: проверьте, нет ли подставленных имён")
        return {"rows": rows, "total": total, "counts": counts, "offset": offset}

    def reconcile(self, known):
        # An updated catalog can already contain a discovery made by another player.
        # Retain the local history, but do not offer it as a new discovery again.
        with self.lock, self.db:
            rows = self.db.execute("SELECT id,src_hash FROM inbox WHERE state='pending' AND origin!='human' AND context=''").fetchall()
            for key, src_hash in rows:
                if known.classify(key, src_hash) is None:
                    self.db.execute("UPDATE inbox SET state='catalogued',revision=revision+1 WHERE id=? AND src_hash=?", (key, src_hash))

    def edit(self, key, src_hash, revision, ru, context, state):
        check_text(ru, "Перевод")
        check_text(context, "Контекст")
        if state not in {"pending", "ignored"}:
            raise ValueError("Недопустимый статус")
        with self.lock, self.db:
            old = self.db.execute("SELECT * FROM inbox WHERE id=? AND src_hash=?", (key, src_hash)).fetchone()
            if not old or old["revision"] != revision:
                raise ValueError("Строка изменилась. Обновите список перед сохранением")
            origin = "human" if ru != old["ru"] else old["origin"]
            if old["state"] == "submitted" and ru == old["ru"] and context == old["context"]:
                raise ValueError("Этот вариант уже отправлен")
            self.db.execute("""UPDATE inbox SET ru=?,context=?,origin=?,state=?,revision=revision+1
                WHERE id=? AND src_hash=?""", (ru, context, origin, state, key, src_hash))

    def prepare(self, selected, version, repository=""):
        if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_BATCH:
            raise ValueError(f"Выберите от 1 до {MAX_BATCH} строк")
        rows, revisions, identities = [], [], set()
        with self.lock:
            for item in selected:
                identity = item["id"], item["src_hash"]
                if identity in identities:
                    raise ValueError("Строка выбрана дважды")
                identities.add(identity)
                row = self.db.execute("SELECT * FROM inbox WHERE id=? AND src_hash=?", identity).fetchone()
                if not row or row["state"] != "pending" or row["revision"] != item["revision"]:
                    raise ValueError("Выбранные строки изменились. Обновите список и проверьте выбор")
                rows.append({name: row[name] for name in ("id", "src_hash", "en", "ru", "origin", "context", "game_build")})
                rows[-1]["status"] = "draft"
                revisions.append([*identity, row["revision"]])
            rows.sort(key=lambda r: (r["id"], r["src_hash"]))
            payload = {"format": 1, "kind": "game-contribution", "language": "ru",
                       "catalog_version": version, "strings": rows}
            payload["batch"] = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:24]
            validate_batch(payload)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO submissions(batch,repository,payload,revisions) VALUES(?,?,?,?)",
                    (payload["batch"], repository, canonical(payload), canonical(revisions)))
        return payload

    def mark_submitted(self, batch, repository, url):
        with self.lock, self.db:
            stored = self.db.execute("SELECT revisions FROM submissions WHERE batch=? AND repository=?", (batch, repository)).fetchone()
            if not stored:
                raise ValueError("Пакет не найден")
            for key, src_hash, revision in json.loads(stored[0]):
                self.db.execute("UPDATE inbox SET state='submitted',pr_url=? WHERE id=? AND src_hash=? AND revision=?",
                                (url, key, src_hash, revision))
            self.db.execute("UPDATE submissions SET state='submitted',pr_url=? WHERE batch=? AND repository=?", (url, batch, repository))


class Collector:
    """Bounded asynchronous writer; capture precedes model/cache lookup and persists on restart."""
    def __init__(self, directory: Path, known_path: Path, game_build="", version=""):
        self.directory, self.known_path, self.game_build = directory, known_path, game_build
        self.version = version
        self.events = queue.Queue(maxsize=4096)
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.enabled = False
        self.dropped = 0
        self.thread = threading.Thread(target=self.run, name="contribution-inbox", daemon=True)
        self.thread.start()

    def put(self, event):
        if not self.stopping.is_set():
            try:
                self.events.put_nowait(event)
            except queue.Full:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 1000 == 0:
                    log.warning("contribution inbox busy; %d events skipped", self.dropped)

    def capture(self, key, src_hash, en):
        self.put(("source", key, f"{src_hash:016x}", en))

    def translated(self, rows):
        for key, src_hash, en, ru in rows:
            if en != ru:
                self.put(("translation", key, f"{src_hash:016x}", ru))

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=5)

    def run(self):
        inbox = known = None
        try:
            inbox, known = Inbox(self.directory), KnownIndex(self.known_path)
            inbox.reconcile(known)
            last_config, last_heartbeat = 0, 0
            self.ready.set()
            while not self.stopping.is_set() or not self.events.empty():
                if time.monotonic() - last_config >= 0.5:
                    self.enabled = inbox.settings()["enabled"]
                    last_config = time.monotonic()
                if time.monotonic()-last_heartbeat >= 5:
                    with inbox.lock, inbox.db:
                        inbox.db.executemany("INSERT OR REPLACE INTO settings VALUES(?,?)", [
                            ("collector_seen", str(int(time.time()))), ("collector_version", self.version),
                            ("collector_dropped", str(self.dropped))])
                    last_heartbeat = time.monotonic()
                try:
                    event = self.events.get(timeout=0.1)
                except queue.Empty:
                    continue
                events = [event]
                while len(events) < 128:
                    try:
                        events.append(self.events.get_nowait())
                    except queue.Empty:
                        break
                try:
                    with inbox.lock, inbox.db:
                        for operation, key, src_hash, text in events:
                            if not self.enabled:
                                continue
                            if operation == "translation":
                                if not catalog.CONTROLS.search(text) and len(text.encode("utf-8")) <= MAX_TEXT:
                                    inbox.translation(key, src_hash, text)
                                continue
                            if (type(key) is not int or not 0 < key < 2**32 or key in TEST_IDS
                                or not text.strip() or len(text.encode("utf-8")) > MAX_TEXT
                                or catalog.CONTROLS.search(text) or src_hash != catalog.source_hash(text)):
                                continue
                            kind = known.classify(key, src_hash)
                            if kind:
                                inbox.capture(key, src_hash, text, kind, self.game_build)
                except sqlite3.Error:
                    log.exception("contribution batch could not be saved")
        except Exception:
            log.exception("local contribution collection unavailable; translation continues")
        finally:
            self.enabled = False
            self.ready.set()
            if known:
                known.close()
            if inbox:
                with inbox.lock, inbox.db:
                    inbox.db.execute("INSERT OR REPLACE INTO settings VALUES('collector_seen','0')")
                inbox.close()
