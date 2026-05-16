#!/usr/bin/python3

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, cast

import aiohttp
import requests


LINES_CHANGED_CACHE_PATH = Path("generated/lines_changed_cache.json")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _build_lines_changed_cache(
    additions: int, deletions: int, now: Optional[datetime] = None
) -> Dict[str, Any]:
    cached_at = _utc_now() if now is None else now.astimezone(timezone.utc)
    return {
        "additions": additions,
        "deletions": deletions,
        "cached_at": cached_at.isoformat(),
    }


def _parse_lines_changed_cache(
    cached: Any, now: Optional[datetime] = None
) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]], str]:
    current_time = _utc_now() if now is None else now.astimezone(timezone.utc)
    if not isinstance(cached, dict):
        return None, None, "invalid_format"

    try:
        additions = int(cached["additions"])
        deletions = int(cached["deletions"])
    except (KeyError, TypeError, ValueError):
        return None, None, "invalid_counts"

    parsed = (additions, deletions)
    fallback = parsed if additions + deletions > 0 else None
    cached_at = cached.get("cached_at")

    if fallback is None:
        return None, None, "zero_total"
    if not isinstance(cached_at, str):
        return None, fallback, "missing_cached_at"

    try:
        cached_time = datetime.fromisoformat(cached_at)
    except ValueError:
        return None, fallback, "invalid_cached_at"

    if cached_time.tzinfo is None:
        cached_time = cached_time.replace(tzinfo=timezone.utc)
    else:
        cached_time = cached_time.astimezone(timezone.utc)

    if cached_time.date() != current_time.date():
        return None, fallback, "stale_day"

    return parsed, fallback, "fresh"


###############################################################################
# Main Classes
###############################################################################


class Queries(object):
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 10,
    ):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }
        try:
            async with self.semaphore:
                r_async = await self.session.post(
                    "https://api.github.com/graphql",
                    headers=headers,
                    json={"query": generated_query},
                )
            result = await r_async.json()
            if result is not None:
                return result
        except:
            print("aiohttp failed for GraphQL query")
            # Fall back on non-async requests
            async with self.semaphore:
                r_requests = requests.post(
                    "https://api.github.com/graphql",
                    headers=headers,
                    json={"query": generated_query},
                )
                result = r_requests.json()
                if result is not None:
                    return result
        return dict()

    async def query_rest(self, path: str, params: Optional[Dict] = None, max_retries: int = 10) -> Any:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :param max_retries: Maximum number of retries on 202 (default: 10)
        :return: deserialized REST JSON output
        """

        for attempt in range(max_retries):
            headers = {
                "Authorization": f"token {self.access_token}",
            }
            if params is None:
                params = dict()
            if path.startswith("/"):
                path = path[1:]
            try:
                async with self.semaphore:
                    r_async = await self.session.get(
                        f"https://api.github.com/{path}",
                        headers=headers,
                        params=tuple(params.items()),
                    )
                if r_async.status == 202:
                    if attempt < max_retries - 1:
                        delay = min(2 ** attempt, 10)  # exponential backoff, max 10s
                        await asyncio.sleep(delay)
                        continue
                    else:
                        break

                result = await r_async.json()
                if result is not None:
                    return result
            except:
                # Fall back on non-async requests
                try:
                    async with self.semaphore:
                        r_requests = requests.get(
                            f"https://api.github.com/{path}",
                            headers=headers,
                            params=tuple(params.items()),
                        )
                        if r_requests.status_code == 202:
                            if attempt < max_retries - 1:
                                delay = min(2 ** attempt, 10)
                                await asyncio.sleep(delay)
                                continue
                            else:
                                break
                        elif r_requests.status_code == 200:
                            return r_requests.json()
                except:
                    pass

        return dict()

    @staticmethod
    def repos_overview(
        contrib_cursor: Optional[str] = None, owned_cursor: Optional[str] = None
    ) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    login,
    name,
    repositories(
        first: 100,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        isFork: false,
        after: {"null" if owned_cursor is None else '"'+ owned_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        contributionTypes: [
            COMMIT,
            PULL_REQUEST,
            REPOSITORY,
            PULL_REQUEST_REVIEW
        ]
        after: {"null" if contrib_cursor is None else '"'+ contrib_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats(object):
    """
    Retrieve and store statistics about GitHub usage.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        exclude_repos: Optional[Set] = None,
        exclude_langs: Optional[Set] = None,
        ignore_forked_repos: bool = False,
        additional_usernames: Optional[Set[str]] = None,
    ):
        self.username = username
        self._additional_usernames = additional_usernames or set()
        self._ignore_forked_repos = ignore_forked_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)

        self._name: Optional[str] = None
        self._stargazers: Optional[int] = None
        self._forks: Optional[int] = None
        self._total_contributions: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._lines_changed: Optional[Tuple[int, int]] = None
        self._views: Optional[int] = None
        self._login: Optional[str] = None

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        self._stargazers = 0
        self._forks = 0
        self._languages = dict()
        self._repos = set()

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(
                    owned_cursor=next_owned, contrib_cursor=next_contrib
                )
            )
            raw_results = raw_results if raw_results is not None else {}

            self._login = (
                raw_results.get("data", {})
                .get("viewer", {})
                .get("login", self.username)
            )
            self._name = raw_results.get("data", {}).get("viewer", {}).get("name", None)
            if self._name is None:
                self._name = self._login

            contrib_repos = (
                raw_results.get("data", {})
                .get("viewer", {})
                .get("repositoriesContributedTo", {})
            )
            owned_repos = (
                raw_results.get("data", {}).get("viewer", {}).get("repositories", {})
            )

            repos = owned_repos.get("nodes", [])
            if not self._ignore_forked_repos:
                repos += contrib_repos.get("nodes", [])

            for repo in repos:
                if repo is None:
                    continue
                name = repo.get("nameWithOwner")
                if name in self._repos or name in self._exclude_repos:
                    continue
                self._repos.add(name)
                self._stargazers += repo.get("stargazers").get("totalCount", 0)
                self._forks += repo.get("forkCount", 0)

                for lang in repo.get("languages", {}).get("edges", []):
                    name = lang.get("node", {}).get("name", "Other")
                    languages = await self.languages
                    if name.lower() in exclude_langs_lower:
                        continue
                    if name in languages:
                        languages[name]["size"] += lang.get("size", 0)
                        languages[name]["occurrences"] += 1
                    else:
                        languages[name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color"),
                        }

            if owned_repos.get("pageInfo", {}).get(
                "hasNextPage", False
            ) or contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get(
                    "endCursor", next_owned
                )
                next_contrib = contrib_repos.get("pageInfo", {}).get(
                    "endCursor", next_contrib
                )
            else:
                break

        # TODO: Improve languages to scale by number of contributions to
        #       specific filetypes
        langs_total = sum([v.get("size", 0) for v in self._languages.values()])
        for k, v in self._languages.items():
            v["prop"] = 100 * (v.get("size", 0) / langs_total)

    @property
    async def name(self) -> str:
        """
        :return: GitHub user's name (e.g., Jacob Strieb)
        """
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert self._name is not None
        return self._name

    @property
    async def stargazers(self) -> int:
        """
        :return: total number of stargazers on user's repos
        """
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert self._stargazers is not None
        return self._stargazers

    @property
    async def forks(self) -> int:
        """
        :return: total number of forks on user's repos
        """
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert self._forks is not None
        return self._forks

    @property
    async def languages(self) -> Dict:
        """
        :return: summary of languages used by the user
        """
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        if self._languages is None:
            await self.get_stats()
            assert self._languages is not None

        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def login(self) -> str:
        """
        :return: GitHub login (username) of the authenticated user
        """
        if self._login is not None:
            return self._login
        await self.get_stats()
        assert self._login is not None
        return self._login

    @property
    async def repos(self) -> Set[str]:
        """
        :return: list of names of user's repos
        """
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub
        """
        if self._total_contributions is not None:
            return self._total_contributions

        self._total_contributions = 0
        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        by_year = (
            (await self.queries.query(Queries.all_contribs(years)))
            .get("data", {})
            .get("viewer", {})
            .values()
        )
        for year in by_year:
            self._total_contributions += year.get("contributionCalendar", {}).get(
                "totalContributions", 0
            )
        return cast(int, self._total_contributions)

    @property
    async def lines_changed(self) -> Tuple[int, int]:
        """
        :return: count of total lines added, removed, or modified by the user
        Uses file caching to avoid repeated API calls across CI runs.
        """
        if self._lines_changed is not None:
            return self._lines_changed

        cache_path = LINES_CHANGED_CACHE_PATH
        fallback_cache = None
        try:
            if cache_path.exists():
                with cache_path.open("r") as f:
                    cached = json.load(f)
                cached_value, fallback_cache, cache_status = _parse_lines_changed_cache(
                    cached
                )
                if cached_value is not None:
                    self._lines_changed = cached_value
                    return self._lines_changed
                print(f"Lines changed cache ignored: {cache_status}.")
        except Exception as exc:
            print(f"Failed to read lines changed cache: {exc}")

        repos = await self.repos

        # Build set of names to match: viewer.login + GITHUB_ACTOR + additional
        match_names = {
            name
            for name in ({await self.login, self.username} | self._additional_usernames)
            if name
        }

        async def query_repo(repo: str) -> Dict[str, Any]:
            """Query a single repo's contributor stats in parallel."""
            r = await self.queries.query_rest(
                f"/repos/{repo}/stats/contributors",
                max_retries=8,
            )
            result = {
                "repo": repo,
                "stats_ready": isinstance(r, list),
                "matched": False,
                "additions": 0,
                "deletions": 0,
            }
            if not isinstance(r, list):
                return result

            repo_additions = 0
            repo_deletions = 0
            for author_obj in r:
                if not isinstance(author_obj, dict) or not isinstance(
                    author_obj.get("author", {}), dict
                ):
                    continue
                author = author_obj.get("author", {}).get("login", "")
                if author not in match_names:
                    continue
                result["matched"] = True
                for week in author_obj.get("weeks", []):
                    repo_additions += week.get("a", 0)
                    repo_deletions += week.get("d", 0)
            result["additions"] = repo_additions
            result["deletions"] = repo_deletions
            return result

        # Query all repos in parallel
        results = await asyncio.gather(*[query_repo(r) for r in repos])

        additions = sum(cast(int, r["additions"]) for r in results)
        deletions = sum(cast(int, r["deletions"]) for r in results)
        stats_ready_count = sum(1 for r in results if r["stats_ready"])
        matched_repo_count = sum(1 for r in results if r["matched"])
        nonzero_repo_count = sum(
            1 for r in results if r["additions"] or r["deletions"]
        )

        print(
            "Lines changed diagnostics: "
            f"repos={len(repos)}, "
            f"stats_ready={stats_ready_count}, "
            f"matched_repos={matched_repo_count}, "
            f"nonzero_repos={nonzero_repo_count}, "
            f"match_names={sorted(match_names)}"
        )

        if additions + deletions > 0:
            self._lines_changed = (additions, deletions)

            try:
                os.makedirs(cache_path.parent, exist_ok=True)
                with cache_path.open("w") as f:
                    json.dump(_build_lines_changed_cache(additions, deletions), f)
            except Exception as exc:
                print(f"Failed to write lines changed cache: {exc}")
            return self._lines_changed

        pending_repos = [r["repo"] for r in results if not r["stats_ready"]][:5]
        unmatched_repos = [
            r["repo"] for r in results if r["stats_ready"] and not r["matched"]
        ][:5]
        if pending_repos:
            print(
                "Contributor stats unavailable for sample repos: "
                + ", ".join(cast(List[str], pending_repos))
            )
        if unmatched_repos:
            print(
                "No matching contributor login found for sample repos: "
                + ", ".join(cast(List[str], unmatched_repos))
            )

        if fallback_cache is not None:
            print(
                "GitHub contributor stats returned zero lines changed; "
                "reusing the previous non-zero cache."
            )
            self._lines_changed = fallback_cache
            return self._lines_changed

        print(
            "GitHub contributor stats returned zero lines changed and "
            "no non-zero cache is available."
        )
        self._lines_changed = (0, 0)

        return self._lines_changed

    @property
    async def views(self) -> int:
        """
        Note: only returns views for the last 14 days (as-per GitHub API)
        :return: total number of page views the user's projects have received
        """
        if self._views is not None:
            return self._views

        total = 0
        for repo in await self.repos:
            r = await self.queries.query_rest(f"/repos/{repo}/traffic/views")
            for view in r.get("views", []):
                total += view.get("count", 0)

        self._views = total
        return total


###############################################################################
# Main Function
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())
