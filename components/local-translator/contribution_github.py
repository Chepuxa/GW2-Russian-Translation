"""Submit one reviewed local snapshot using the player's GitHub CLI identity."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, urlencode

from contribution_store import canonical, repository_name, validate_batch


class GitHubError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, module_dir: Path):
        bundled = module_dir / "tools/github/gh.exe"
        if not bundled.is_file() and getattr(sys, "frozen", False):
            bundled = Path(sys.executable).parent / "tools/github/gh.exe"
        self.executable = str(bundled) if bundled.is_file() else shutil.which("gh")
        self.directory = module_dir
        self.login_process = None

    def api(self, method, endpoint, data=None):
        if not self.executable:
            raise GitHubError("GitHub CLI отсутствует. Установите gh с cli.github.com или обновите пакет переводчика")
        command = [self.executable, "api", "--hostname", "github.com", "--method", method,
                   "-H", "Accept: application/vnd.github+json", endpoint]
        if data is not None:
            command += ["--input", "-"]
        environment = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
        try:
            result = subprocess.run(command, input=canonical(data).encode("utf-8") if data is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.directory, env=environment,
                timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise GitHubError("GitHub не ответил вовремя. Пакет сохранён локально; отправку можно повторить") from exc
        if result.returncode:
            match = re.search(rb"HTTP (\d{3})", result.stderr)
            status = int(match[1]) if match else 0
            # Never forward raw subprocess output, tokens, or account configuration to the page/logs.
            message = {401: "Войдите в GitHub заново", 403: "GitHub отклонил запрос: проверьте права аккаунта и лимит API",
                       404: "Репозиторий или объект не найден на GitHub", 422: "GitHub отклонил изменение: проверьте репозиторий и существующее предложение"}.get(status,
                       "Запрос к GitHub не выполнен. Проверьте вход в аккаунт и подключение")
            raise GitHubError(message, status)
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (ValueError, UnicodeError) as exc:
            raise GitHubError("GitHub вернул неожиданный ответ") from exc

    def account(self):
        if not self.executable:
            return {"available": False, "login": "", "message": "GitHub CLI отсутствует"}
        try:
            return {"available": True, "login": self.api("GET", "user")["login"], "message": ""}
        except GitHubError as exc:
            return {"available": True, "login": "", "message": str(exc)}

    def login(self):
        if not self.executable:
            raise GitHubError("Установите GitHub CLI с https://cli.github.com/")
        if os.name != "nt":
            raise GitHubError("Выполните gh auth login --web в терминале, затем обновите статус")
        if self.login_process and self.login_process.poll() is None:
            return
        self.login_process = subprocess.Popen([self.executable, "auth", "login", "--hostname", "github.com",
            "--git-protocol", "https", "--web"], cwd=self.directory,
            creationflags=subprocess.CREATE_NEW_CONSOLE)

    def submit(self, document, repository):
        validate_batch(document)
        repository = repository_name(repository)
        actor = self.api("GET", "user")["login"]
        upstream = self.api("GET", f"repos/{repository}")
        if upstream.get("private") or upstream.get("archived") or upstream.get("disabled"):
            raise GitHubError("Выберите действующий публичный репозиторий проекта")
        base = upstream["default_branch"]
        try:
            marker = self.api("GET", f"repos/{repository}/contents/tools/contributions.py?"+urlencode({"ref": base}))
            if marker.get("type") != "file":
                raise GitHubError("В репозитории отсутствует механизм приёма драфтов")
        except GitHubError as exc:
            if exc.status == 404:
                raise GitHubError("Сначала опубликуйте GW2-Russian-Translation: в выбранном репозитории нет tools/contributions.py") from exc
            raise

        base_head = self.api("GET", f"repos/{repository}/git/ref/heads/{quote(base, safe='')}")["object"]["sha"]
        if upstream["owner"]["login"].casefold() == actor.casefold():
            fork_name = repository
        else:
            fork = self.api("POST", f"repos/{repository}/forks", {"default_branch_only": True})
            parent = fork.get("parent", {}).get("full_name", "")
            if not fork.get("fork") or parent.casefold() != repository.casefold() or fork["owner"]["login"].casefold() != actor.casefold():
                raise GitHubError("GitHub не подтвердил принадлежность fork этому проекту")
            fork_name = repository_name(fork["full_name"])
        # Fork creation is asynchronous on GitHub. Wait only for this repository's base object.
        commit = None
        for attempt in range(15):
            try:
                commit = self.api("GET", f"repos/{fork_name}/git/commits/{base_head}")
                break
            except GitHubError as exc:
                if exc.status not in {404, 409} or attempt == 14:
                    raise
                time.sleep(1)
        branch = "community/ru-" + document["batch"]
        path = "contributions/ru/" + document["batch"] + ".json"
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        endpoint = f"repos/{fork_name}/git/ref/heads/{quote(branch, safe='')}"
        try:
            self.api("GET", endpoint)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            tree = self.api("POST", f"repos/{fork_name}/git/trees", {"base_tree": commit["tree"]["sha"],
                "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}]})
            new_commit = self.api("POST", f"repos/{fork_name}/git/commits", {
                "message": f"Add {len(document['strings'])} Russian translation drafts", "tree": tree["sha"], "parents": [base_head]})
            try:
                self.api("POST", f"repos/{fork_name}/git/refs", {"ref": "refs/heads/" + branch, "sha": new_commit["sha"]})
            except GitHubError as race:
                if race.status != 422:
                    raise
        # Retry safely: never overwrite an existing branch, and verify its exact contribution.
        remote = self.api("GET", f"repos/{fork_name}/contents/{path}?"+urlencode({"ref": branch}))
        if remote.get("encoding") != "base64" or base64.b64decode(remote["content"]).decode("utf-8") != content:
            raise GitHubError("Ветка уже содержит другой пакет. Она оставлена без изменений")
        head = actor + ":" + branch
        query = urlencode({"head": head, "base": base, "state": "all"})
        existing = self.api("GET", f"repos/{repository}/pulls?"+query)
        if existing:
            return existing[0]["html_url"]
        body = (f"Предложено {len(document['strings'])} строк из локального сбора игры.\n\n"
            "Все записи имеют статус `draft`. Переводы модели помечены отдельно от предложений участника. "
            "Состав пакета просмотрен отправителем перед публикацией.\n\n"
            "Проверка: `python tools/contributions.py check`. Принятие пакета не присваивает статус reviewed.")
        try:
            pull = self.api("POST", f"repos/{repository}/pulls", {"title": f"RU: {len(document['strings'])} новых драфтов",
                "head": head, "base": base, "body": body})
            return pull["html_url"]
        except GitHubError as exc:
            if exc.status == 422:
                existing = self.api("GET", f"repos/{repository}/pulls?"+query)
                if existing:
                    return existing[0]["html_url"]
            raise

    def submit_changes(self, document, repository):
        """Publish an exact multi-file snapshot; reject upstream changes before creating a branch."""
        from contribution_editor import validate_changes, digest
        validate_changes(document)
        repository = repository_name(repository)
        actor = self.api("GET", "user")["login"]
        upstream = self.api("GET", f"repos/{repository}")
        if upstream.get("private") or upstream.get("archived") or upstream.get("disabled"):
            raise GitHubError("Выберите действующий публичный репозиторий проекта")
        base = upstream["default_branch"]
        marker = self.api("GET", f"repos/{repository}/contents/tools/catalog.py?" + urlencode({"ref": base}))
        if marker.get("type") != "file":
            raise GitHubError("В репозитории отсутствует каталог GW2-Russian-Translation")
        base_head = self.api("GET", f"repos/{repository}/git/ref/heads/{quote(base, safe='')}")["object"]["sha"]
        branch = "community/edit-ru-" + document["batch"]
        if upstream["owner"]["login"].casefold() == actor.casefold():
            fork_name = repository
        else:
            fork = self.api("POST", f"repos/{repository}/forks", {"default_branch_only": True})
            if (not fork.get("fork") or fork.get("parent", {}).get("full_name", "").casefold() != repository.casefold()
                    or fork["owner"]["login"].casefold() != actor.casefold()):
                raise GitHubError("GitHub не подтвердил принадлежность fork этому проекту")
            fork_name = repository_name(fork["full_name"])
        endpoint = f"repos/{fork_name}/git/ref/heads/{quote(branch, safe='')}"
        try:
            self.api("GET", endpoint)
            exists = True
        except GitHubError as exc:
            if exc.status != 404:
                raise
            exists = False
        if not exists:
            for item in document["files"]:
                try:
                    remote = self.api("GET", f"repos/{repository}/contents/{quote(item['path'], safe='/')}?" + urlencode({"ref": base_head}))
                except GitHubError as exc:
                    if exc.status != 404 or item["base_sha256"] is not None:
                        raise GitHubError("Исходный файл изменился или отсутствует на GitHub: " + item["path"]) from exc
                else:
                    if (remote.get("encoding") != "base64" or item["base_sha256"] is None or
                            digest(base64.b64decode(remote["content"])) != item["base_sha256"]):
                        raise GitHubError("На GitHub другая версия файла: " + item["path"] + ". Обновите локальную копию проекта и перенесите правки")
            for attempt in range(15):
                try:
                    commit = self.api("GET", f"repos/{fork_name}/git/commits/{base_head}")
                    break
                except GitHubError as exc:
                    if exc.status not in {404, 409} or attempt == 14:
                        raise
                    time.sleep(1)
            tree = self.api("POST", f"repos/{fork_name}/git/trees", {"base_tree": commit["tree"]["sha"], "tree": [
                {"path": item["path"], "mode": "100644", "type": "blob", "content": item["content"]}
                for item in document["files"]]})
            new_commit = self.api("POST", f"repos/{fork_name}/git/commits", {
                "message": f"Propose Russian translation changes ({len(document['files'])} files)",
                "tree": tree["sha"], "parents": [base_head]})
            try:
                self.api("POST", f"repos/{fork_name}/git/refs", {"ref": "refs/heads/" + branch, "sha": new_commit["sha"]})
            except GitHubError as exc:
                if exc.status != 422:
                    raise
        for item in document["files"]:
            remote = self.api("GET", f"repos/{fork_name}/contents/{quote(item['path'], safe='/')}?" + urlencode({"ref": branch}))
            if remote.get("encoding") != "base64" or base64.b64decode(remote["content"]).decode("utf-8") != item["content"]:
                raise GitHubError("Ветка содержит другие изменения. Она оставлена без изменений")
        query = urlencode({"head": actor + ":" + branch, "base": base, "state": "all"})
        existing = self.api("GET", f"repos/{repository}/pulls?" + query)
        if existing:
            return existing[0]["html_url"]
        body = ("Предложения из локальной мастерской перевода. Изменённые строки требуют редакторской проверки.\n\n" +
                "\n".join("- `" + item["path"] + "`" for item in document["files"]) +
                "\n\nПроверка: `python tools/catalog.py check`, `python tools/drafts.py check`. "
                "Панель не присваивает предложениям статус reviewed.")
        comparison = self.api("GET", f"repos/{repository}/compare/{base_head}...{actor}:{quote(branch, safe='')}?per_page=1")
        changed = comparison.get("files", [])
        if ({r["filename"] for r in changed} != {r["path"] for r in document["files"]} or
                any(r.get("status") not in {"added", "modified"} for r in changed)):
            raise GitHubError("Ветка содержит посторонние изменения. PR не создан")
        try:
            return self.api("POST", f"repos/{repository}/pulls", {"title": "RU: правки перевода из мастерской",
                "head": actor + ":" + branch, "base": base, "body": body})["html_url"]
        except GitHubError as exc:
            if exc.status == 422:
                existing = self.api("GET", f"repos/{repository}/pulls?" + query)
                if existing:
                    return existing[0]["html_url"]
            raise
