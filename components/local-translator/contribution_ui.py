"""Loopback-only contribution viewer. Every upload is an explicit snapshot selection."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import re
import webbrowser

from contribution_github import GitHub
from contribution_store import Inbox, KnownIndex
from contribution_editor import Editor, project_root

log = logging.getLogger(__name__)


class Panel:
    def __init__(self, module_dir, known_path=None, inbox_dir=None, github=None, project=None):
        self.module_dir = module_dir
        self.inbox = Inbox(inbox_dir or module_dir / "RU/contributions")
        self.known_path = known_path or module_dir / "community/known-strings.db"
        self.version, self.index_available = "", False
        try:
            known = KnownIndex(self.known_path)
            self.version, self.index_available = known.version, True
            self.inbox.reconcile(known)
            known.close()
        except Exception:
            log.warning("Contribution known-string index unavailable")
        self.github = github or GitHub(module_dir)
        self.token = secrets.token_urlsafe(32)
        self.csp_nonce = secrets.token_urlsafe(24)
        self.job_lock = threading.Lock()
        self.job = {"state": "idle", "url": "", "message": ""}
        root = project_root(project) if project else None
        self.editor = Editor(root, self.inbox.directory, self.inbox) if root else None

    def status(self):
        with self.job_lock:
            job = dict(self.job)
        with self.inbox.lock:
            counts = dict(self.inbox.db.execute("SELECT state,count(*) FROM inbox GROUP BY state"))
        return {"settings": self.inbox.settings(), "catalog_version": self.version,
                "index_available": self.index_available, "counts": counts, "job": job,
                "collector": self.inbox.collector_status(),
                "editor": {"available": self.editor is not None, "project": str(self.editor.root) if self.editor else ""}}

    def submit_changes(self, selected, repository, confirmed):
        if confirmed is not True:
            raise ValueError("Подтвердите просмотр и публикацию выбранных изменений")
        if not self.editor:
            raise ValueError("Откройте панель через Contribute.cmd из папки проекта")
        if not repository or repository != self.inbox.settings()["repository"]:
            raise ValueError("Сохраните адрес репозитория перед отправкой")
        with self.job_lock:
            if self.job["state"] == "running":
                raise ValueError("Предыдущая отправка ещё выполняется")
            document = self.editor.prepare(selected)
            self.job = {"state": "running", "url": "", "message": "Отправляются правки каталога…"}

        def work():
            try:
                url = self.github.submit_changes(document, repository)
                if not re.fullmatch(r"https://github\.com/" + re.escape(repository) + r"/pull/\d+", url, flags=re.I):
                    raise ValueError("GitHub вернул неожиданный адрес предложения")
                self.editor.mark_submitted(document, url)
                result = {"state": "complete", "url": url, "message": "Предложение отправлено. Локальные правки сохранены до обновления проекта"}
            except Exception as exc:
                result = {"state": "failed", "url": "", "message": str(exc)}
            with self.job_lock:
                self.job = result
        threading.Thread(target=work, daemon=True, name="catalog-submit").start()
        return {"started": True, "batch": document["batch"]}

    def submit(self, selected, repository, confirmed):
        if confirmed is not True:
            raise ValueError("Подтвердите просмотр и отсутствие личных данных в выбранных строках")
        if not repository or repository != self.inbox.settings()["repository"]:
            raise ValueError("Сохраните адрес репозитория и проверьте его перед отправкой")
        if not self.index_available:
            raise ValueError("Каталог отсутствует. Обновите пакет переводчика")
        with self.job_lock:
            if self.job["state"] == "running":
                raise ValueError("Предыдущая отправка ещё выполняется")
            document = self.inbox.prepare(selected, self.version, repository)
            self.job = {"state": "running", "url": "", "message": "Создаётся предложение на GitHub…"}

        def work():
            try:
                url = self.github.submit(document, repository)
                if not re.fullmatch(r"https://github\.com/"+re.escape(repository)+r"/pull/\d+", url, flags=re.I):
                    raise ValueError("GitHub вернул неожиданный адрес предложения")
                self.inbox.mark_submitted(document["batch"], repository, url)
                result = {"state": "complete", "url": url, "message": "Предложение отправлено"}
            except Exception as exc:
                result = {"state": "failed", "url": "", "message": str(exc)}
            with self.job_lock:
                self.job = result

        threading.Thread(target=work, daemon=True, name="contribution-submit").start()
        return {"started": True, "batch": document["batch"]}


def make_server(panel, port=0):
    asset_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    page = (asset_dir / "contribution_ui.html").read_text(encoding="utf-8")
    editor_script = (asset_dir / "contribution_editor.js").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "GW2-Community/1"

        def log_message(self, *_):
            pass  # No originals, credentials, or secret URLs in HTTP logs.

        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            encoded = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{panel.csp_nonce}'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(encoded)

        def handle_request(self, post=False):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != origin[7:] or self.headers.get("Origin", origin) != origin:
                self.respond(403, {"error": "Доступ разрешён только из локальной панели"})
                return
            parsed = urlsplit(self.path)
            if not post and parsed.path == "/":
                self.respond(200, page.replace("__NONCE__", panel.csp_nonce), "text/html; charset=utf-8")
                return
            if not post and parsed.path == "/editor.js":
                self.respond(200, editor_script, "text/javascript; charset=utf-8")
                return
            authorization = self.headers.get("Authorization", "")
            if not secrets.compare_digest(authorization, "Bearer " + panel.token):
                self.respond(403, {"error": "Откройте панель через Contribute.cmd"})
                return
            try:
                if post:
                    if not self.headers.get("Content-Type", "").startswith("application/json"):
                        raise ValueError("Требуется JSON")
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 200000:
                        raise ValueError("Некорректный размер запроса")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("Требуется объект")
                    if parsed.path == "/api/settings":
                        if data.get("enabled") and not panel.index_available:
                            raise ValueError("Нужен каталог известных строк. Обновите пакет переводчика")
                        result = panel.inbox.configure(data["enabled"], data["repository"])
                    elif parsed.path == "/api/edit":
                        panel.inbox.edit(data["id"], data["src_hash"], data["revision"], data["ru"], data["context"], data["state"])
                        result = {"saved": True}
                    elif parsed.path == "/api/export":
                        if not panel.index_available:
                            raise ValueError("Каталог отсутствует. Обновите пакет")
                        result = panel.inbox.prepare(data["selected"], panel.version)
                    elif parsed.path == "/api/login":
                        panel.github.login()
                        result = {"started": True}
                    elif parsed.path == "/api/submit":
                        result = panel.submit(data["selected"], data["repository"], data.get("confirmed"))
                    elif parsed.path.startswith("/api/editor/"):
                        if not panel.editor:
                            raise ValueError("Откройте панель через Contribute.cmd из папки проекта")
                        action = parsed.path.removeprefix("/api/editor/")
                        if action == "create":
                            result = panel.editor.create(data["path"], data.get("directory", False), data.get("section", ""))
                        elif action == "glossary/save":
                            result = panel.editor.save_term(data.get("identity"), data["revision"], data["en"], data["ru"], data["context"])
                        elif action == "glossary/delete":
                            result = panel.editor.delete_term(data["identity"], data["revision"])
                        elif action == "save":
                            result = panel.editor.save(data["source"], data["identity"], data["revision"], data["ru"], data["context"])
                        elif action == "promote":
                            result = panel.editor.promote(data["source"], data["identity"], data["revision"], data["ru"], data["context"], data["game_id"], data["path"])
                        elif action == "export":
                            result = panel.editor.prepare(data["selected"])
                        elif action == "submit":
                            result = panel.submit_changes(data["selected"], data["repository"], data.get("confirmed"))
                        else:
                            raise ValueError("Неизвестное действие редактора")
                    elif parsed.path == "/api/shutdown":
                        with panel.job_lock:
                            if panel.job["state"] == "running":
                                raise ValueError("Дождитесь окончания отправки")
                        result = {"closed": True}
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        self.respond(404, {"error": "Страница не найдена"})
                        return
                elif parsed.path == "/api/status":
                    result = panel.status()
                elif parsed.path == "/api/account":
                    result = panel.github.account()
                elif parsed.path == "/api/rows":
                    args = parse_qs(parsed.query)
                    result = panel.inbox.listing(args.get("state", ["pending"])[0], args.get("search", [""])[0], args.get("offset", [0])[0])
                elif parsed.path.startswith("/api/editor/"):
                    if not panel.editor:
                        raise ValueError("Откройте панель через Contribute.cmd из папки проекта")
                    args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    action = parsed.path.removeprefix("/api/editor/")
                    if action == "tree":
                        result = panel.editor.tree()
                    elif action == "glossary":
                        result = panel.editor.glossary_listing(**args)
                    elif action == "rows":
                        result = panel.editor.listing(**args)
                    elif action == "changes":
                        result = panel.editor.changes()
                    else:
                        raise ValueError("Неизвестный раздел редактора")
                else:
                    self.respond(404, {"error": "Страница не найдена"})
                    return
                self.respond(200, result)
            except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                log.exception("Contribution panel request failed")
                self.respond(500, {"error": "Не удалось выполнить действие. Локальная очередь сохранена"})

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(post=True)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_module = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    parser.add_argument("--module-dir", type=Path, default=default_module)
    parser.add_argument("--known-index", type=Path)
    parser.add_argument("--inbox-dir", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    panel = Panel(args.module_dir, args.known_index, args.inbox_dir, project=project_root(args.project_root))
    server = make_server(panel)
    url = f"http://127.0.0.1:{server.server_port}/#{panel.token}"
    if args.state_file:
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        args.state_file.write_text(json.dumps({"url": url}), encoding="utf-8")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        panel.inbox.close()
        if panel.editor:
            panel.editor.close()
        if args.state_file:
            args.state_file.unlink(missing_ok=True)
    return 0
