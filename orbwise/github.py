"""GitHub-Anbindung für den Coding-Modus: Repos auflisten und klonen, Branches pushen.

Token (in dieser Reihenfolge): github.token in der Config, $ORBWISE_GITHUB_TOKEN / $GH_TOKEN / $GITHUB_TOKEN, sonst
das Token einer angemeldeten GitHub-CLI (`gh auth token`). Das Token steht nie in einer URL oder in .git/config:
git bekommt es für den einen Aufruf über GIT_ASKPASS (ein kleines Skript, das es aus der Umgebung liest).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .config import env

log = logging.getLogger(__name__)

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


async def branches(cfg: Any, full_name: str, transport=None) -> list[str]:
    """Branches eines Repos, der Standard-Branch zuerst."""
    full_name = _repo_name(full_name)
    info = await _get(cfg, f"/repos/{full_name}", transport=transport)
    names = [b.get("name", "") for b in await _get(cfg, f"/repos/{full_name}/branches", {"per_page": 100}, transport)]
    default = info.get("default_branch") or ""
    return ([default] if default in names else []) + sorted(n for n in names if n and n != default)


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


def repo_of(url: str) -> str | None:
    """owner/name aus einer github.com-URL."""
    m = _REMOTE.search(url.strip())
    return m.group("repo") if m else None


def remote_repo(path: str) -> str | None:
    """owner/name, wenn origin auf github.com zeigt."""
    try:
        return repo_of(git(["remote", "get-url", "origin"], cwd=path))
    except (GitHubError, OSError, subprocess.SubprocessError):
        return None


def sandbox_origin(sb: dict) -> str:
    """Remote einer Sandbox-Arbeitskopie: gemerkt beim Anlegen, sonst aus dem Original-Projekt – nie aus dem .git der
    Kopie (das kann der Container umschreiben, z. B. auf einen fremden Server, der dann das Token bekäme)."""
    if sb.get("origin") is not None:
        return str(sb["origin"])
    try:
        return git(["remote", "get-url", "origin"], cwd=sb.get("project") or None).strip()
    except (GitHubError, OSError, subprocess.SubprocessError):
        return ""


def clone_dir(cfg: Any) -> Path:
    raw = str(getattr(getattr(cfg, "github", None), "clone_dir", "") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    home = Path.home()
    for name in ("Projekte", "Projects", "projects", "git", "code"):
        if (home / name).is_dir():
            return home / name
    return home / ("Projects" if getattr(cfg, "language", "de") == "en" else "Projekte")


def _repo_name(full_name: str) -> str:
    full_name = full_name.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
        raise GitHubError(f"'{full_name}' ist kein Repo-Name (owner/name).")
    return full_name


def clone(cfg: Any, full_name: str, url: str | None = None, branch: str = "") -> str:
    """Klont owner/name nach clone_dir (gibt es den Ordner schon: fetch) und checkt branch aus (leer: Standard bzw.
    der aktuelle). Liefert den Pfad."""
    full_name = _repo_name(full_name)
    branch = branch.strip()
    if branch:
        git(["check-ref-format", "--branch", branch])
    dest = clone_dir(cfg) / full_name.split("/", 1)[1]
    tok = token(cfg)
    if (dest / ".git").is_dir():
        git(["fetch", "--prune", "origin"], cwd=str(dest), tok=tok)
        if branch and branch != current_branch(str(dest)):
            if git(["status", "--porcelain", "--untracked-files=no"], cwd=str(dest)).strip():
                raise GitHubError(f"{dest} hat ungespeicherte Änderungen – erst committen oder verwerfen, dann auf "
                                  f"„{branch}“ wechseln.")
            git(["checkout", "--quiet", branch], cwd=str(dest))
        if branch:
            try:
                git(["pull", "--ff-only", "--quiet"], cwd=str(dest), tok=tok)
            except GitHubError as e:
                log.info("pull --ff-only in %s: %s", dest, e)
        return str(dest)
    if dest.exists() and any(dest.iterdir()):
        raise GitHubError(f"{dest} gibt es schon und ist kein Git-Repo.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", *(["--branch", branch] if branch else []), url or repo_url(full_name), str(dest)], tok=tok)
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
    return _links(branch, repo)


def push_from_copy(cfg: Any, ws: str, origin: str, branch: str = "") -> dict:
    """Push aus der Sandbox-Arbeitskopie, ohne git in deren .git laufen zu lassen: Hooks, core.fsmonitor oder ein
    umgebogenes Remote dort stammen womöglich vom Agenten im Container. Der Branch wird in ein frisches Repo geholt
    (nur Objekte) und von dort zur gemerkten URL gepusht."""
    from . import workspace
    branch = branch.strip() or workspace.head_branch(ws) or ""
    if not branch:
        raise GitHubError("Kein Branch ausgecheckt (detached HEAD).")
    if not origin:
        raise GitHubError("Das Projekt hat kein Remote „origin“.")
    git(["check-ref-format", "--branch", branch])
    gd = workspace._git_dir(ws)
    if gd is None:
        raise GitHubError("Die Arbeitskopie ist kein Git-Repo.")
    ref = f"refs/heads/{branch}"
    with tempfile.TemporaryDirectory(prefix="orbwise-push-") as tmp:
        git(["init", "-q", "--bare", tmp])
        git([*workspace.SAFE_GIT, "fetch", "-q", "--no-tags", str(gd), f"+{ref}:{ref}"], cwd=tmp)
        git([*workspace.SAFE_GIT, "push", origin, f"{ref}:{ref}"], cwd=tmp, tok=token(cfg), timeout=300)
    return _links(branch, repo_of(origin))


def _links(branch: str, repo: str | None) -> dict:
    out = {"branch": branch, "repo": repo}
    if repo:
        out["url"] = f"https://github.com/{repo}/tree/{branch}"
        out["compare"] = f"https://github.com/{repo}/compare/{branch}?expand=1"
    return out


# ---------------------------------------------------------------- Projekt nachträglich mit einem Repo verbinden
def has_commits(path: str) -> bool:
    try:
        git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=path)
        return True
    except (GitHubError, OSError, subprocess.SubprocessError):
        return False


def is_repo(path: str) -> bool:
    try:
        return git(["rev-parse", "--show-toplevel"], cwd=path).strip() == str(Path(path).resolve())
    except (GitHubError, OSError, subprocess.SubprocessError):
        return False


def connect(path: str, full_name: str) -> dict:
    """Projektordner mit owner/name verbinden: notfalls `git init`, dann origin eintragen. Ein anderes origin wird nie
    still umgebogen."""
    full_name = _repo_name(full_name)
    url = repo_url(full_name)
    if not is_repo(path):
        git(["init", "-q", "-b", "main"], cwd=path)
    try:
        current = git(["config", "--get", "remote.origin.url"], cwd=path).strip()
    except GitHubError:
        current = ""
    if current and repo_of(current) != full_name:
        raise GitHubError(f"Das Projekt hat schon das Remote origin {current} – erst von Hand ändern "
                          f"(git remote set-url origin {url}).")
    if not current:
        git(["remote", "add", "origin", url], cwd=path)
    return {"repo": full_name, "branch": current_branch(path) if has_commits(path) else "main"}


def first_commit(path: str, message: str = "", identity: dict | None = None) -> dict:
    """Alles committen, was noch nicht im Repo ist (.gitignore gilt) – Dateien mit Zugangsdaten (.env, Schlüssel …)
    bleiben draußen und werden gemeldet. Liefert {committed, files, skipped}."""
    from .tools.secretpaths import is_secret_path
    git(["add", "-A"], cwd=path)
    staged = [n for n in git(["diff", "--cached", "--name-only", "-z"], cwd=path).split("\0") if n]
    skipped = [n for n in staged if is_secret_path(Path(path) / n)]
    if skipped:
        unstage = ["reset", "-q", "--"] if has_commits(path) else ["rm", "-q", "--cached", "--"]
        git([*unstage, *skipped], cwd=path)
    files = len(staged) - len(skipped)
    if not files:
        return {"committed": False, "files": 0, "skipped": skipped}
    ident = []
    try:
        has_ident = bool(git(["config", "user.email"], cwd=path).strip())
    except GitHubError:
        has_ident = False
    if identity and not has_ident:
        ident = ["-c", f"user.name={identity.get('name') or identity.get('login') or 'Orbwise'}",
                 "-c", f"user.email={identity.get('email') or 'orbwise@localhost'}"]
    git([*ident, "commit", "-q", "-m", message.strip() or "Erster Commit"], cwd=path)
    return {"committed": True, "files": files, "skipped": skipped}



def connect_and_push(cfg: Any, project: str, full_name: str, sb: dict | None = None, message: str = "",
                     identity: dict | None = None) -> dict:
    """Projekt mit einem (meist frisch angelegten, leeren) Repo verbinden und hochladen. Ohne Commit wird vorher alles
    als erster Commit festgehalten. Mit Sandbox geht der Branch der Arbeitskopie hoch; ihr gemerktes origin
    (sb["origin"]) wird gesetzt – speichern muss der Aufrufer."""
    if sb:
        from . import workspace
        if workspace._git_dir(sb["path"]) is None:
            raise GitHubError("Die Arbeitskopie der Sandbox ist kein Git-Repo – erst „In Projekt übernehmen“, die "
                              "Sandbox beenden und dann verbinden.")
    info = connect(project, full_name)
    commit = {"committed": False, "files": 0, "skipped": []}
    try:
        if sb:
            sb["origin"] = repo_url(info["repo"])
            out = push_from_copy(cfg, sb["path"], sb["origin"])
        else:
            if not has_commits(project):
                commit = first_commit(project, message, identity)
                if not commit["committed"]:
                    raise GitHubError("Im Projekt ist nichts zum Hochladen (leer oder alles in .gitignore).")
            out = push(cfg, project)
    except GitHubError as e:
        if "fetch first" in str(e) or "non-fast-forward" in str(e):
            raise GitHubError(f"github.com/{info['repo']} ist nicht leer – dort liegt schon etwas, das hier fehlt "
                              "(z. B. eine README). Erst holen und zusammenführen: git pull origin <branch> "
                              "--allow-unrelated-histories.") from e
        raise
    return {**out, **_links(out["branch"], info["repo"]), "files": commit["files"], "skipped": commit["skipped"]}
