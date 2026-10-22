"""Local repository editor. Originals are immutable; publication uses explicit snapshots."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import threading

import catalog
import glossary as glossary_data
from contribution_store import canonical, check_text, formatting_ok, TEST_IDS, validate_batch


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(document):
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def project_root(explicit=None):
    if explicit:
        root = Path(explicit).resolve(strict=True)
        if not (root / "drafts/ru").is_dir() or not (root / "translations/ru").is_dir():
            raise ValueError("Выберите папку проекта с drafts/ru и translations/ru")
        return root
    for parent in Path(__file__).resolve().parents:
        if (parent / "drafts/ru").is_dir() and (parent / "translations/ru").is_dir():
            return parent
    return None


def safe_path(root, name, allow_directory=False):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("Некорректный путь каталога")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or ".." in path.parts:
        raise ValueError("Некорректный путь каталога")
    parts = path.parts
    allowed = len(parts) >= 2 and parts[:2] == ("translations", "ru")
    draft = len(parts) == 3 and parts[:2] == ("drafts", "ru") and re.fullmatch(r"p-[0-9a-f]{2}\.json", parts[-1])
    glossary_file = name == glossary_data.PATH
    if not allowed and not draft and not glossary_file:
        raise ValueError("Изменять можно только translations/ru, файлы drafts/ru и glossary/ru.json")
    for part in parts[2:]:
        if (part in {".", ".."} or part.endswith((".", " ")) or
            not re.fullmatch(r"[\w .-]{1,100}", part) or
            part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"{x}{i}" for x in ("COM", "LPT") for i in range(1, 10)]}):
            raise ValueError("Некорректное имя папки или файла")
    if not allow_directory and not glossary_file and (len(parts) < 3 or path.suffix != ".json"):
        raise ValueError("Укажите JSON-файл внутри translations/ru")
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Путь выходит за пределы проекта")
    return target


def validate_changes(document):
    if not isinstance(document, dict) or set(document) != {"format", "kind", "batch", "files"}:
        raise ValueError("Неверный формат изменений")
    if type(document["format"]) is not int or document["format"] != 1 or document["kind"] != "catalog-changes" or not isinstance(document["files"], list) or not 1 <= len(document["files"]) <= 100:
        raise ValueError("Выберите от 1 до 100 файлов")
    seen = set()
    for item in document["files"]:
        if set(item) != {"path", "base_sha256", "content"}:
            raise ValueError("Неверный формат файла")
        name = item["path"]
        if name.endswith("/.gitkeep"):
            if not name.startswith("translations/ru/"):
                raise ValueError("Неверный путь папки")
            safe_path(Path.cwd(), name.rsplit("/", 1)[0], True)
            if item["content"] != "":
                raise ValueError("Неверный маркер папки")
        else:
            safe_path(Path.cwd(), name)
            parsed = json.loads(item["content"], object_pairs_hook=catalog.unique_object)
            if name == glossary_data.PATH:
                glossary_data.validate(parsed)
        if name.casefold() in seen or len(item["content"].encode("utf-8")) > 5_000_000:
            raise ValueError("Повтор файла или превышен размер")
        seen.add(name.casefold())
        if item["base_sha256"] is not None and not re.fullmatch("[0-9a-f]{64}", item["base_sha256"]):
            raise ValueError("Неверная исходная версия файла")
    unsigned = {k: v for k, v in document.items() if k != "batch"}
    if document["batch"] != digest(canonical(unsigned).encode())[:24]:
        raise ValueError("Контрольная сумма изменений не совпадает")
    return document


class Editor:
    def __init__(self, root, directory, inbox):
        self.root = Path(root).resolve()
        self.inbox = inbox
        directory.mkdir(parents=True, exist_ok=True)
        suffix = digest(str(self.root).casefold().encode())[:12]
        self.db = sqlite3.connect(directory / f"catalog-{suffix}.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("casefold", 1, lambda s: s.casefold(), deterministic=True)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,stamp TEXT,revision TEXT);
            CREATE TABLE IF NOT EXISTS rows(source TEXT,identity TEXT,file TEXT,en TEXT,ru TEXT,
                context TEXT,game_id TEXT,data TEXT,PRIMARY KEY(source,identity));
            CREATE INDEX IF NOT EXISTS rows_file ON rows(file);
            CREATE INDEX IF NOT EXISTS rows_hash ON rows(json_extract(data,'$.src_hash')) WHERE source='translations';
            CREATE TABLE IF NOT EXISTS changes(path TEXT PRIMARY KEY,base BLOB);
            CREATE TABLE IF NOT EXISTS submissions(batch TEXT PRIMARY KEY,payload TEXT,url TEXT);
        """)
        self.lock = threading.RLock()
        try:
            self.refresh()
        except Exception:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def _read(self, path):
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=catalog.unique_object)

    def refresh(self):
        with self.lock, self.db:
            paths = (list((self.root / "drafts/ru").glob("p-*.json")) +
                     list((self.root / "translations/ru").rglob("*.json")) +
                     list((self.root / "contributions/ru").glob("*.json")))
            existing = set()
            for path in sorted(paths):
                name = path.relative_to(self.root).as_posix()
                existing.add(name)
                stat = path.stat()
                stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
                before = self.db.execute("SELECT stamp FROM files WHERE path=?", (name,)).fetchone()
                if before and before[0] == stamp:
                    continue
                raw = path.read_bytes()
                doc = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=catalog.unique_object)
                source = name.split("/")[0]
                if source == "contributions":
                    validate_batch(doc)
                self.db.execute("DELETE FROM rows WHERE file=?", (name,))
                values = []
                for row in doc["strings"]:
                    identity = row.get("key") if source == "drafts" else str(row["id"])
                    if source == "contributions":
                        identity = name + ":" + str(row["id"]) + ":" + row["src_hash"]
                    values.append((source, identity, name, row["en"], row["ru"], row.get("context", ""),
                                   " ".join(map(str, row.get("ids", [row.get("id", "")]))), canonical(row)))
                self.db.executemany("INSERT INTO rows VALUES(?,?,?,?,?,?,?,?)", values)
                self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (name, stamp, digest(raw)))
            for row in self.db.execute("SELECT path FROM files").fetchall():
                if row[0] not in existing:
                    self.db.execute("DELETE FROM rows WHERE file=?", (row[0],))
                    self.db.execute("DELETE FROM files WHERE path=?", (row[0],))
            self.db.execute("DELETE FROM rows WHERE source='local'")
            with self.inbox.lock:
                local = self.inbox.db.execute("SELECT * FROM inbox WHERE state!='ignored'").fetchall()
            self.db.executemany("INSERT INTO rows VALUES(?,?,?,?,?,?,?,?)", [
                ("local", f"{r['id']}:{r['src_hash']}", "", r["en"], r["ru"], r["context"], str(r["id"]), canonical(dict(r))) for r in local])

    def tree(self):
        with self.lock:
            return [{"path": p.relative_to(self.root).as_posix(), "directory": p.is_dir()}
                    for p in sorted((self.root / "translations/ru").rglob("*"))
                    if p.is_dir() or p.suffix == ".json"]

    def _glossary(self):
        path = safe_path(self.root, glossary_data.PATH)
        raw = path.read_bytes() if path.exists() else None
        document = (json.loads(raw.decode("utf-8-sig"), object_pairs_hook=catalog.unique_object)
                    if raw is not None else dict(glossary_data.DEFAULT, terms=[]))
        glossary_data.validate(document)
        return document, digest(raw) if raw is not None else None

    def glossary_listing(self, search="", offset=0):
        with self.lock:
            document, revision = self._glossary()
            query = search[:200].strip().casefold()
            terms = [dict(term, identity=glossary_data.identity(term)) for term in document["terms"]
                     if query in " ".join(term.values()).casefold()]
            terms.sort(key=lambda term: (term["en"].casefold(), term["context"].casefold()))
            offset = max(0, int(offset))
            return {"rows": terms[offset:offset + 30], "total": len(terms), "count": len(document["terms"]),
                    "offset": offset, "limit": 30, "revision": revision, "file": glossary_data.PATH}

    def save_term(self, identity, revision, en, ru, context):
        with self.lock:
            document, current = self._glossary()
            if current != revision:
                raise ValueError("Словарь изменился. Обновите список перед сохранением")
            term = {"en": en, "ru": ru, "context": context}
            glossary_data.validate({"format": 1, "language": "ru", "terms": [term]})
            term = {field: value.strip() for field, value in term.items()}
            if identity is None:
                document["terms"].append(term)
            else:
                found = next((i for i, item in enumerate(document["terms"]) if glossary_data.identity(item) == identity), None)
                if found is None:
                    raise ValueError("Термин больше не найден. Обновите список")
                if document["terms"][found] == term:
                    return {"saved": True, "revision": current}
                document["terms"][found] = term
            glossary_data.validate(document)
            self._write(glossary_data.PATH, encode(document), current)
            return {"saved": True, "revision": self._glossary()[1]}

    def delete_term(self, identity, revision):
        with self.lock:
            document, current = self._glossary()
            if revision != current:
                raise ValueError("Словарь изменился. Обновите список перед удалением")
            found = next((i for i, item in enumerate(document["terms"]) if glossary_data.identity(item) == identity), None)
            if found is None:
                raise ValueError("Термин больше не найден. Обновите список")
            removed = document["terms"].pop(found)
            self._write(glossary_data.PATH, encode(document), current)
            return {"deleted": True, "term": removed, "revision": self._glossary()[1]}

    def _public(self, row):
        data = json.loads(row["data"])
        revision = (self.db.execute("SELECT revision FROM files WHERE path=?", (row["file"],)).fetchone()[0]
                    if row["file"] else str(data["revision"]))
        h = data.get("key", data.get("src_hash"))
        links = [dict(r) for r in self.db.execute(
            "SELECT file,game_id FROM rows WHERE source='translations' AND json_extract(data,'$.src_hash')=?", (h,))]
        return dict(data, source=row["source"], identity=row["identity"], file=row["file"], revision=revision,
                    linked=links, formatting_ok=formatting_ok(data["en"], data["ru"]))

    def listing(self, view="drafts", search="", state="all", offset=0, path="", source="all"):
        if view not in {"drafts", "translations"} or state not in {"all", "empty", "translated", "machine", "human", "formatting", "unlinked"}:
            raise ValueError("Неизвестный фильтр")
        with self.lock:
            self.refresh()
            clause = "source='translations'" if view == "translations" else "source!='translations'"
            params = []
            if source != "all" and view == "drafts":
                if source not in {"drafts", "contributions", "local"}:
                    raise ValueError("Неизвестный источник")
                clause += " AND source=?"
                params.append(source)
            if path:
                safe_path(self.root, path, True)
                clause += " AND (file=? OR substr(file,1,?)=?)"
                params += [path, len(path) + 1, path + "/"]
            if search:
                clause += " AND (instr(casefold(en||' '||ru||' '||context),casefold(?))>0 OR instr(game_id,?)>0)"
                params += [search[:200], search[:200]]
            if state == "empty":
                clause += " AND trim(ru)=''"
            elif state == "translated":
                clause += " AND trim(ru)!=''"
            elif state == "machine":
                clause += " AND json_extract(data,'$.origin') IN ('machine','machine-cache')"
            elif state == "human":
                clause += " AND json_extract(data,'$.origin')='human'"
            elif state == "unlinked":
                clause += " AND NOT EXISTS(SELECT 1 FROM rows t WHERE t.source='translations' AND json_extract(t.data,'$.src_hash')=COALESCE(json_extract(rows.data,'$.key'),json_extract(rows.data,'$.src_hash')))"
            elif state == "formatting":
                self.db.create_function("formatting_ok", 2, formatting_ok, deterministic=True)
                clause += " AND NOT formatting_ok(en,ru)"
            count = self.db.execute("SELECT count(*) FROM rows WHERE " + clause, params).fetchone()[0]
            offset = max(0, int(offset))
            found = self.db.execute("SELECT * FROM rows WHERE " + clause + " ORDER BY source,identity LIMIT 30 OFFSET ?", params + [offset]).fetchall()
            return {"total": count, "rows": [self._public(r) for r in found], "offset": offset, "limit": 30}

    def get(self, source, identity):
        self.refresh()
        row = self.db.execute("SELECT * FROM rows WHERE source=? AND identity=?", (source, str(identity))).fetchone()
        if not row:
            raise ValueError("Строка больше не найдена. Обновите список")
        return self._public(row)

    def _write(self, name, raw, expected):
        target = safe_path(self.root, name[:-9] if name.endswith("/.gitkeep") else name, name.endswith("/.gitkeep"))
        if name.endswith("/.gitkeep"):
            target = target / ".gitkeep"
        current = target.read_bytes() if target.exists() else None
        revision = digest(current) if current is not None else None
        if revision != expected:
            raise ValueError("Файл изменился. Обновите список перед сохранением")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO changes VALUES(?,?)", (name, current))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".workshop-tmp")
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self.refresh()

    def create(self, name, directory=False, section=""):
        with self.lock:
            target = safe_path(self.root, name, directory)
            if not name.startswith("translations/ru/") or target.exists():
                raise ValueError("Укажите новое имя внутри translations/ru")
            if directory:
                self._write(name + "/.gitkeep", b"", None)
            else:
                section = section.strip() or name.removeprefix("translations/ru/").removesuffix(".json")
                doc = {"format": 1, "language": "ru", "section": section, "strings": []}
                catalog.load_catalog(self.root, {name: doc})
                self._write(name, encode(doc), None)
            return {"saved": True, "path": name}

    def save(self, source, identity, revision, ru, context):
        check_text(ru, "Перевод")
        check_text(context, "Контекст")
        with self.lock:
            row = self.get(source, identity)
            if row["revision"] != revision:
                raise ValueError("Строка изменилась. Обновите список перед сохранением")
            if not formatting_ok(row["en"], ru):
                raise ValueError("Сохраните теги и подстановки оригинала или очистите перевод")
            if source == "local":
                self.inbox.edit(row["id"], row["src_hash"], int(revision), ru, context, "pending")
            elif source == "contributions":
                raise ValueError("Присланный пакет сохраняет историю. Используйте «Добавить в переводы»")
            else:
                doc = self._read(safe_path(self.root, row["file"]))
                for item in doc["strings"]:
                    if str(item.get("key", item.get("id"))) == str(identity):
                        if item["ru"] == ru and item["context"] == context:
                            return {"saved": True}
                        item.update(ru=ru, context=context)
                        if source == "translations":
                            check_text(context, "Контекст", empty=False)
                            item["status"] = "draft"
                        else:
                            item["origin"] = "human"
                        break
                if source == "translations":
                    catalog.load_catalog(self.root, {row["file"]: doc})
                self._write(row["file"], encode(doc), revision)
            return {"saved": True}

    def promote(self, source, identity, revision, ru, context, game_id, path):
        check_text(ru, "Перевод", empty=False)
        check_text(context, "Контекст", empty=False)
        if type(game_id) is not int or not 0 < game_id < 2**32 or game_id in TEST_IDS:
            raise ValueError("Укажите настоящий игровой ID")
        with self.lock:
            row = self.get(source, identity)
            if source == "translations" or row["revision"] != revision:
                raise ValueError("Обновите строку перед переносом")
            ids = row.get("ids", [row.get("id")])
            if ids and game_id not in ids:
                raise ValueError("ID не соответствует выбранному оригиналу")
            if not formatting_ok(row["en"], ru):
                raise ValueError("Сохраните теги и подстановки оригинала")
            conflict = self.db.execute("SELECT file FROM rows WHERE source='translations' AND identity=?", (str(game_id),)).fetchone()
            if conflict:
                raise ValueError("Этот ID уже находится в " + conflict[0] + ". Исправьте существующую запись")
            target = safe_path(self.root, path)
            if not path.startswith("translations/ru/"):
                raise ValueError("Выберите файл translations/ru")
            doc = self._read(target) if target.exists() else {"format": 1, "language": "ru", "section": path[16:-5], "strings": []}
            old = digest(target.read_bytes()) if target.exists() else None
            doc["strings"].append({"id": game_id, "en": row["en"], "ru": ru, "context": context,
                "src_hash": catalog.source_hash(row["en"]), "status": "draft",
                "evidence": "Community editor: " + (row["file"] or "local game capture"),
                "verification": "Editor and screen review pending."})
            catalog.load_catalog(self.root, {path: doc})
            self._write(path, encode(doc), old)
            return {"saved": True, "path": path, "id": game_id}

    def changes(self):
        with self.lock:
            result = []
            for row in self.db.execute("SELECT * FROM changes ORDER BY path").fetchall():
                target = self.root / row["path"]
                raw = target.read_bytes() if target.is_file() else None
                if raw is None or raw == row["base"]:
                    continue
                before = row["base"].decode("utf-8-sig") if row["base"] is not None else ""
                after = raw.decode("utf-8-sig")
                result.append({"path": row["path"], "base_sha256": digest(row["base"]) if row["base"] is not None else None,
                    "revision": digest(raw), "content": after,
                    "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                        fromfile="a/" + row["path"], tofile="b/" + row["path"])), "new": row["base"] is None})
            return result

    def prepare(self, selected):
        if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
            raise ValueError("Выберите от 1 до 100 изменённых файлов")
        with self.lock:
            available = {r["path"]: r for r in self.changes()}
            files = []
            for choice in selected:
                row = available.get(choice["path"])
                if not row or row["revision"] != choice["revision"]:
                    raise ValueError("Состав правок изменился. Просмотрите изменения заново")
                files.append({k: row[k] for k in ("path", "base_sha256", "content")})
            # Check the entire effective catalog, not only individual edited rows.
            catalog.load_catalog(self.root)
            unsigned = {"format": 1, "kind": "catalog-changes", "files": sorted(files, key=lambda f: f["path"])}
            document = dict(unsigned, batch=digest(canonical(unsigned).encode())[:24])
            validate_changes(document)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO submissions VALUES(?,?,NULL)", (document["batch"], canonical(document)))
            return document

    def mark_submitted(self, document, url):
        with self.lock, self.db:
            self.db.execute("UPDATE submissions SET url=? WHERE batch=?", (url, document["batch"]))
        # Keep the base and editable files: a PR is a proposal, not a merged update.
