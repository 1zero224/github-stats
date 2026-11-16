#!/usr/bin/python3

"""
Compute language statistics from commits authored by the user.

GitHub's repository ``languages`` field measures the size of every file in a
repository. That includes code the user never wrote, such as the upstream
history of a detached fork, vendored dependencies, generated notebooks, or
documentation imported from elsewhere. This module instead walks only the
commits that GitHub attributes to the user and counts the lines they added,
grouped by the language of the touched file.

Repositories are cloned with ``--filter=blob:none`` so that only the blobs
needed to diff the user's own commits are downloaded. Results are cached per
repository in ``generated/authored_languages_cache.json`` and invalidated when
the set of the user's authored commits on the default branch changes.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from github_stats import Stats


AUTHORED_LANGUAGES_CACHE_PATH = Path("generated/authored_languages_cache.json")
CACHE_VERSION = 1
CLONE_CONCURRENCY = 4
GIT_TIMEOUT_SECONDS = 1200
_REST_RETRIES = 3


# GitHub Linguist language names keyed by lowercase file extension. Languages
# that are not listed here are reported as "Other", matching GitHub's
# behaviour for unrecognized file types.
LANGUAGES_BY_EXTENSION: Dict[str, str] = {
    ".asm": "Assembly",
    ".bash": "Shell",
    ".bat": "Batchfile",
    ".c": "C",
    ".cc": "C++",
    ".cfg": "INI",
    ".clj": "Clojure",
    ".cljs": "Clojure",
    ".cmake": "CMake",
    ".cmd": "Batchfile",
    ".cpp": "C++",
    ".cr": "Crystal",
    ".cs": "C#",
    ".css": "CSS",
    ".cxx": "C++",
    ".dart": "Dart",
    ".dockerfile": "Dockerfile",
    ".elm": "Elm",
    ".erl": "Erlang",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".frag": "GLSL",
    ".fs": "F#",
    ".fsx": "F#",
    ".glsl": "GLSL",
    ".go": "Go",
    ".gradle": "Groovy",
    ".groovy": "Groovy",
    ".h": "C",
    ".hcl": "HCL",
    ".hh": "C++",
    ".hpp": "C++",
    ".hrl": "Erlang",
    ".hs": "Haskell",
    ".htm": "HTML",
    ".html": "HTML",
    ".hxx": "C++",
    ".ini": "INI",
    ".ino": "C++",
    ".ipynb": "Jupyter Notebook",
    ".java": "Java",
    ".jl": "Julia",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".less": "Less",
    ".lua": "Lua",
    ".m": "Objective-C",
    ".md": "Markdown",
    ".mjs": "JavaScript",
    ".ml": "OCaml",
    ".mli": "OCaml",
    ".mm": "Objective-C",
    ".nim": "Nim",
    ".php": "PHP",
    ".pl": "Perl",
    ".plsql": "PLpgSQL",
    ".pm": "Perl",
    ".proto": "Protocol Buffer",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
    ".py": "Python",
    ".r": "R",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".rst": "reStructuredText",
    ".s": "Assembly",
    ".sass": "Sass",
    ".scala": "Scala",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".sol": "Solidity",
    ".sql": "SQL",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".tex": "TeX",
    ".tf": "HCL",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".v": "V",
    ".vert": "GLSL",
    ".vue": "Vue",
    ".wgsl": "WGSL",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".zig": "Zig",
    ".zsh": "Shell",
}

# Extension-less files that Linguist still recognizes.
LANGUAGES_BY_FILENAME: Dict[str, str] = {
    "cmakelists.txt": "CMake",
    "dockerfile": "Dockerfile",
    "gemfile": "Ruby",
    "gnumakefile": "Makefile",
    "jenkinsfile": "Groovy",
    "makefile": "Makefile",
    "podfile": "Ruby",
    "procfile": "Procfile",
    "rakefile": "Ruby",
    "vagrantfile": "Ruby",
}

# Fallback colours for languages that are not part of the top ten of any
# repository, so the generated badge still looks reasonable.
FALLBACK_COLORS: Dict[str, str] = {
    "C": "#555555",
    "C#": "#178600",
    "C++": "#f34b7d",
    "CMake": "#DA3434",
    "CSS": "#563d7c",
    "Dart": "#00B4AB",
    "Dockerfile": "#384d54",
    "Go": "#00ADD8",
    "HTML": "#e34c26",
    "Java": "#b07219",
    "JavaScript": "#f1e05a",
    "JSON": "#292929",
    "Jupyter Notebook": "#DA5B0B",
    "Kotlin": "#A97BFF",
    "Less": "#1d365d",
    "Makefile": "#427819",
    "Markdown": "#083fa1",
    "PHP": "#4F5D95",
    "PowerShell": "#012456",
    "Python": "#3572A5",
    "Ruby": "#701516",
    "Rust": "#dea584",
    "SCSS": "#c6538c",
    "Shell": "#89e051",
    "SQL": "#e38c00",
    "Swift": "#F05138",
    "TeX": "#3D6117",
    "TypeScript": "#3178c6",
    "Vue": "#41b883",
    "YAML": "#cb171e",
}


################################################################################
# Helper Functions
################################################################################


def language_for_path(path: str) -> str:
    """
    :param path: repository-relative file path
    :return: GitHub language name for the file, or "Other" when unknown
    """
    filename = path.rsplit("/", 1)[-1].lower()
    if filename in LANGUAGES_BY_FILENAME:
        return LANGUAGES_BY_FILENAME[filename]

    dot = filename.rfind(".")
    if dot <= 0:
        return "Other"
    return LANGUAGES_BY_EXTENSION.get(filename[dot:], "Other")


def parse_numstat(output: str) -> Dict[str, int]:
    """
    :param output: output of ``git log --numstat --format='%x01%H'``
    :return: added lines keyed by language
    """
    added_by_language: Dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            # Commit header lines (prefixed with \x01) and blank separators.
            continue
        added, _deleted, path = parts
        if not added.isdigit():
            # Binary files are reported with "-" for both counters.
            continue
        language = language_for_path(path)
        added_by_language[language] = added_by_language.get(language, 0) + int(
            added
        )
    return added_by_language


def commit_signature(shas: List[str]) -> str:
    """
    :param shas: authored commit SHAs, newest first
    :return: cache signature that changes when a new authored commit appears
    """
    return f"{len(shas)}:{shas[0] if shas else ''}"


def aggregate_languages(
    scans: Iterable[Tuple[str, Dict[str, int]]],
    colors: Dict[str, Optional[str]],
    excluded_langs: Optional[Set[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    :param scans: (repository, added lines by language) pairs
    :param colors: GitHub language colours keyed by language
    :param excluded_langs: languages to omit from the result
    :return: language summary with the same shape as ``Stats.languages``
    """
    excluded = {x.lower() for x in (excluded_langs or set())}
    totals: Dict[str, int] = {}
    occurrences: Dict[str, int] = {}

    for _repo, languages in scans:
        for language, added in languages.items():
            if added <= 0 or language.lower() in excluded:
                continue
            totals[language] = totals.get(language, 0) + added
            occurrences[language] = occurrences.get(language, 0) + 1

    total = sum(totals.values())
    if total <= 0:
        return {}

    return {
        language: {
            "size": added,
            "occurrences": occurrences[language],
            "color": colors.get(language) or FALLBACK_COLORS.get(language),
            "prop": 100 * added / total,
        }
        for language, added in totals.items()
    }


################################################################################
# Git Helpers
################################################################################


def _run_git(
    args: List[str], input_text: Optional[str] = None
) -> subprocess.CompletedProcess:
    """
    Run git without interactive prompts and capture its output.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args],
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env=env,
    )


def _scrub_token(message: str, token: str) -> str:
    return message.replace(token, "***") if token else message


def _clone_repository(repo: str, token: str, destination: str) -> None:
    """
    Clone a repository without blobs so only authored diffs are downloaded.
    """
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    result = _run_git(
        [
            "clone",
            "--bare",
            "--filter=blob:none",
            "--single-branch",
            "--quiet",
            url,
            destination,
        ]
    )
    if result.returncode != 0:
        detail = _scrub_token(result.stderr.strip(), token)
        raise RuntimeError(f"git clone failed for {repo}: {detail}")


def _count_authored_lines(repo_dir: str, shas: Set[str]) -> Dict[str, int]:
    """
    Count added lines per language across the given commits.
    """
    local = _run_git(["-C", repo_dir, "rev-list", "HEAD"])
    if local.returncode != 0:
        raise RuntimeError(f"git rev-list failed: {local.stderr.strip()}")

    present = set(local.stdout.split())
    target = sorted(shas & present)
    if not target:
        return {}

    log = _run_git(
        [
            "-C",
            repo_dir,
            "-c",
            "core.quotepath=false",
            "log",
            "--no-walk=unsorted",
            "--stdin",
            "--no-merges",
            "--numstat",
            "--no-renames",
            "--format=%x01%H",
        ],
        input_text="\n".join(target) + "\n",
    )
    if log.returncode != 0:
        raise RuntimeError(f"git log failed: {log.stderr.strip()}")
    return parse_numstat(log.stdout)


def _remove_tree(path: str) -> None:
    """
    Remove a temporary clone, making git's read-only objects writable first.

    Windows refuses to delete read-only files, so a plain ``shutil.rmtree``
    would leave the clones behind on developer machines.
    """

    def make_writable(function: Any, target: str, _exception: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=make_writable)


def _scan_repository(repo: str, token: str, shas: Set[str]) -> Dict[str, int]:
    """
    Clone a repository, count the lines authored by the user, then clean up.
    """
    if not shas:
        return {}

    workdir = tempfile.mkdtemp(prefix="github-stats-")
    try:
        repo_dir = os.path.join(workdir, "repo.git")
        _clone_repository(repo, token, repo_dir)
        return _count_authored_lines(repo_dir, shas)
    finally:
        _remove_tree(workdir)


################################################################################
# GitHub REST Helpers
################################################################################


async def _rest_get(
    s: Stats, path: str, params: Dict[str, Any]
) -> Any:
    """
    GET a GitHub REST endpoint, retrying transient rate limit responses.

    :return: decoded JSON, or an empty list for empty/missing repositories
    """
    session = s.queries.session
    if session is None:
        raise RuntimeError("An active HTTP session is required.")

    token = s.queries.access_token
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/{path.lstrip('/')}"

    last_error = "unknown error"
    for attempt in range(_REST_RETRIES):
        async with s.queries.semaphore:
            response = await session.get(url, headers=headers, params=params)
            if response.status == 200:
                return await response.json()
            if response.status in (404, 409):
                # Missing repository or one without any commits.
                return []
            body = await response.text()
            last_error = f"GitHub API {response.status} for {path}: {body[:200]}"
            retry_after = response.headers.get("Retry-After")

        if response.status not in (403, 429) or attempt == _REST_RETRIES - 1:
            break
        delay = float(retry_after) if retry_after else float(2**attempt)
        await asyncio.sleep(delay)

    raise RuntimeError(_scrub_token(last_error, token))


async def _list_owned_repos(s: Stats) -> List[str]:
    """
    :return: every repository owned by the user, including forks

    ``Stats.repos`` only contains non-fork repositories, but authored commits
    in a fork belong to the user just as much as commits in an original
    repository, so forks are added explicitly here.
    """
    repos: List[str] = []
    page = 1
    while True:
        payload = await _rest_get(
            s,
            "user/repos",
            {"affiliation": "owner", "per_page": 100, "page": page},
        )
        if not isinstance(payload, list) or not payload:
            break

        for item in payload:
            if isinstance(item, dict) and item.get("full_name"):
                repos.append(item["full_name"])

        if len(payload) < 100:
            break
        page += 1

    return repos


async def _list_authored_commits(s: Stats, repo: str, login: str) -> List[str]:
    """
    :return: SHAs of commits on the default branch authored by the user,
             newest first
    """
    shas: List[str] = []
    page = 1
    while True:
        payload = await _rest_get(
            s,
            f"repos/{repo}/commits",
            {"author": login, "per_page": 100, "page": page},
        )
        if not isinstance(payload, list) or not payload:
            break

        for item in payload:
            if isinstance(item, dict) and item.get("sha"):
                shas.append(item["sha"])

        if len(payload) < 100:
            break
        page += 1

    return shas


################################################################################
# Cache Helpers
################################################################################


def _load_cache(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    :return: cached per-repository entries, or an empty mapping
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            cached = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"Authored languages cache ignored: {exc}")
        return {}

    if not isinstance(cached, dict) or cached.get("version") != CACHE_VERSION:
        return {}
    repos = cached.get("repos")
    return repos if isinstance(repos, dict) else {}


def _save_cache(
    path: Path, entries: Dict[str, Dict[str, Any]]
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": CACHE_VERSION,
                    "repos": entries,
                },
                f,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
    except Exception as exc:
        print(f"Failed to write authored languages cache: {exc}")


################################################################################
# Public API
################################################################################


async def authored_languages(
    s: Stats,
    excluded_langs: Optional[Set[str]] = None,
    excluded_repos: Optional[Set[str]] = None,
    cache_path: Path = AUTHORED_LANGUAGES_CACHE_PATH,
) -> Dict[str, Dict[str, Any]]:
    """
    :param s: GitHub statistics handle for the authenticated user
    :param excluded_langs: languages to omit from the result
    :param excluded_repos: repositories to omit from the language statistics
    :param cache_path: per-repository cache file
    :return: languages used based on lines added in the user's own commits
    """
    login = await s.login
    try:
        owned_repos = await _list_owned_repos(s)
    except Exception as exc:
        print(f"Authored languages could not list owned repos: {exc}")
        owned_repos = []

    excluded_names = {repo.lower() for repo in (excluded_repos or set())}
    repos = sorted(
        repo
        for repo in set(owned_repos) | set(await s.repos)
        if repo.lower() not in excluded_names
    )
    token = s.queries.access_token
    cached_repos = _load_cache(cache_path)

    semaphore = asyncio.Semaphore(CLONE_CONCURRENCY)
    today = datetime.now(timezone.utc).isoformat()

    async def process_repository(
        repo: str,
    ) -> Tuple[str, Dict[str, int], Optional[str], bool, Optional[str]]:
        """
        :return: repo, languages, signature, whether the cache was reused,
                 and an error message when the repository could not be scanned
        """
        try:
            shas = await _list_authored_commits(s, repo, login)
        except Exception as exc:
            previous = cached_repos.get(repo, {})
            signature = previous.get("signature")
            return repo, previous.get("languages", {}), signature, True, str(exc)

        signature = commit_signature(shas)
        entry = cached_repos.get(repo)
        if isinstance(entry, dict) and entry.get("signature") == signature:
            return repo, entry.get("languages", {}), signature, True, None

        last_error: Optional[str] = None
        for attempt in range(2):
            try:
                async with semaphore:
                    languages = await asyncio.to_thread(
                        _scan_repository, repo, token, set(shas)
                    )
                return repo, languages, signature, False, None
            except Exception as exc:
                # Lazy blob fetches occasionally fail on flaky connections;
                # a fresh clone is cheap enough to retry once.
                last_error = str(exc)
                if attempt == 0:
                    await asyncio.sleep(2)

        previous = cached_repos.get(repo, {})
        return (
            repo,
            previous.get("languages", {}),
            previous.get("signature"),
            True,
            last_error,
        )

    results = await asyncio.gather(
        *(process_repository(repo) for repo in repos)
    )

    new_cache: Dict[str, Dict[str, Any]] = {}
    scans: List[Tuple[str, Dict[str, int]]] = []
    for repo, languages, signature, _reused, error in results:
        if error is not None:
            print(f"Authored languages skipped {repo}: {error}")
            if repo in cached_repos:
                new_cache[repo] = cached_repos[repo]
            continue

        scans.append((repo, languages))
        new_cache[repo] = {
            "signature": signature,
            "languages": languages,
            "scanned_at": today,
        }

    colors = {
        language: data.get("color")
        for language, data in (await s.languages).items()
    }
    summary = aggregate_languages(scans, colors, excluded_langs)

    print(
        "Authored languages diagnostics: "
        f"repos={len(repos)}, "
        f"reused={sum(1 for result in results if result[3])}, "
        f"failed={sum(1 for result in results if result[4] is not None)}, "
        f"languages={len(summary)}"
    )

    _save_cache(cache_path, new_cache)
    return summary
