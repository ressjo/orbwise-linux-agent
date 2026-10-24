"""GitHub-Anbindung für den Coding-Modus: Repos auflisten und klonen, Branches pushen.

Token (in dieser Reihenfolge): github.token in der Config, $ORBWISE_GITHUB_TOKEN / $GH_TOKEN / $GITHUB_TOKEN, sonst
das Token einer angemeldeten GitHub-CLI (`gh auth token`). Das Token steht nie in einer URL oder in .git/config:
git bekommt es für den einen Aufruf über GIT_ASKPASS (ein kleines Skript, das es aus der Umgebung liest).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import env

API = "https://api.github.com"
_REMOTE = re.compile(r"github\.com[:/]+(?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?/?$")
_ASKPASS = """#!/bin/sh
case "$1" in
  Username*) echo "x-access-token" ;;
  *) echo "$ORBWISE_GIT_TOKEN" ;;
esac
"""


class GitHubError(RuntimeError):
    pass


# ---------------------------------------------------------------- Token
_gh_cache: tuple[float, str] | None = None


def token_info(cfg: Any) -> tuple[str, str]:
    """(Token, Quelle) – Quelle: config, env, gh oder leer. `gh auth token` wird eine Minute zwischengespeichert."""
    global _gh_cache
    tok = str(getattr(getattr(cfg, "github", None), "token", "") or "").strip()
    if tok:
        return tok, "config"
    for name in ("ORBWISE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = (env("GITHUB_TOKEN") if name == "ORBWISE_GITHUB_TOKEN" else os.environ.get(name, "")).strip()
        if value:
            return value, "env"
    if _gh_cache and time.monotonic() - _gh_cache[0] < 60:
        return (_gh_cache[1], "gh") if _gh_cache[1] else ("", "")
    found = ""
    gh = shutil.which("gh")
    if gh:
        try:
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                found = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    _gh_cache = (time.monotonic(), found)
    return (found, "gh") if found else ("", "")


def token(cfg: Any) -> str:
    return token_info(cfg)[0]


# ---------------------------------------------------------------- API
def _client(tok: str, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=20, transport=transport,
                             headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Orbwise"})


async def _get(cfg: Any, path: str, params: dict | None = None, transport=None) -> Any:
    tok = token(cfg)
    if not tok:
        raise GitHubError("Kein GitHub-Token – `gh auth login` ausführen oder github.token in der Config setzen.")
    async with _client(tok, transport) as client:
        r = await client.get(path, params=params)
    if r.status_code == 401:
        raise GitHubError("GitHub lehnt das Token ab (abgelaufen oder ohne Rechte).")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub-Fehler {r.status_code}: {r.text[:200]}")
    return r.json()


async def user(cfg: Any, transport=None) -> dict:
    """Login, Name und die noreply-Adresse (für Commits in der Sandbox)."""
    u = await _get(cfg, "/user", transport=transport)
    login = u.get("login", "")
    return {"login": login, "name": u.get("name") or login,
            "email": f"{u.get('id')}+{login}@users.noreply.github.com" if login else ""}


async def repos(cfg: Any, query: str = "", transport=None) -> list[dict]:
    """Eigene Repos (zuletzt gepusht zuerst); mit Suchtext gefiltert – findet nichts, die GitHub-Suche."""
    items = await _get(cfg, "/user/repos", {"sort": "pushed", "per_page": 100,
                                            "affiliation": "owner,collaborator,organization_member"}, transport)
    q = query.strip().lower()
    if q:
        hits = [r for r in items if q in r.get("full_name", "").lower() or q in (r.get("description") or "").lower()]
        if not hits and len(q) >= 2:
            found = await _get(cfg, "/search/repositories", {"q": query, "per_page": 30}, transport)
            hits = found.get("items", [])
        items = hits
    return [{"full_name": r.get("full_name", ""), "private": bool(r.get("private")),
             "description": r.get("description") or "", "pushed_at": r.get("pushed_at") or "",
             "default_branch": r.get("default_branch") or "main"} for r in items]


# ---------------------------------------------------------------- git mit Token
def _askpass() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = Path(base) / "orbwise" / "git-askpass.sh"
    if not path.exists() or path.read_text() != _ASKPASS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_ASKPASS)
        path.chmod(stat.S_IRWXU)
    return str(path)


def git_env(tok: str) -> dict:
    """Umgebung für einen git-Aufruf mit Token – nichts davon landet in der Repo-Konfiguration."""
    return {**os.environ, "GIT_ASKPASS": _askpass(), "ORBWISE_GIT_TOKEN": tok, "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never", "LC_ALL": "C"}


def git(args: list[str], cwd: str | None = None, tok: str = "", timeout: float = 600) -> str:
    r = subprocess.run(["git", "-c", "credential.helper=", *args], cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, env=git_env(tok) if tok else {**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise GitHubError((r.stderr or r.stdout).strip().replace(tok, "***")[-600:] if tok else
                          (r.stderr or r.stdout).strip()[-600:])
    return r.stdout


def repo_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def remote_repo(path: str) -> str | None:
    """owner/name, wenn origin auf github.com zeigt."""
    try:
        url = git(["remote", "get-url", "origin"], cwd=path).strip()
    except (GitHubError, OSError, subprocess.SubprocessError):
        return None
    m = _REMOTE.search(url)
    return m.group("repo") if m else None


def clone_dir(cfg: Any) -> Path:
    raw = str(getattr(getattr(cfg, "github", None), "clone_dir", "") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    home = Path.home()
    for name in ("Projekte", "Projects", "projects", "git", "code"):
        if (home / name).is_dir():
            return home / name
    return home / ("Projects" if getattr(cfg, "language", "de") == "en" else "Projekte")


def clone(cfg: Any, full_name: str, url: str | None = None) -> str:
    """Klont owner/name nach clone_dir (gibt es den Ordner schon: fetch). Liefert den Pfad."""
    full_name = full_name.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
        raise GitHubError(f"'{full_name}' ist kein Repo-Name (owner/name).")
    dest = clone_dir(cfg) / full_name.split("/", 1)[1]
    tok = token(cfg)
    if (dest / ".git").is_dir():
        git(["fetch", "--prune", "origin"], cwd=str(dest), tok=tok)
        return str(dest)
    if dest.exists() and any(dest.iterdir()):
        raise GitHubError(f"{dest} gibt es schon und ist kein Git-Repo.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", url or repo_url(full_name), str(dest)], tok=tok)
    return str(dest)


def current_branch(path: str) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).strip()


def push(cfg: Any, path: str, branch: str = "") -> dict:
    """Pusht den Branch (Standard: den aktuellen) nach origin und liefert Links für den PR."""
    branch = branch.strip() or current_branch(path)
    if branch == "HEAD":
        raise GitHubError("Kein Branch ausgecheckt (detached HEAD).")
    repo = remote_repo(path)
    git(["push", "-u", "origin", f"{branch}:{branch}"], cwd=path, tok=token(cfg), timeout=300)
    out = {"branch": branch, "repo": repo}
    if repo:
        out["url"] = f"https://github.com/{repo}/tree/{branch}"
        out["compare"] = f"https://github.com/{repo}/compare/{branch}?expand=1"
    return out
