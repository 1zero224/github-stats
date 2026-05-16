import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from github_stats import LINES_CHANGED_CACHE_PATH, Stats, _build_lines_changed_cache


async def _read_lines_changed(stats: Stats):
    return await stats.lines_changed


@contextmanager
def temporary_workdir():
    previous = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            yield
        finally:
            os.chdir(previous)


class LinesChangedTests(unittest.TestCase):
    def make_stats(self, additional_usernames=None) -> Stats:
        stats = Stats("actor", "token", None, additional_usernames=additional_usernames)
        stats._repos = {"owner/repo"}
        stats._login = "viewer"
        stats._viewer_id = "viewer-id"
        return stats

    def test_fresh_positive_cache_is_reused(self):
        with temporary_workdir():
            os.makedirs(LINES_CHANGED_CACHE_PATH.parent, exist_ok=True)
            with LINES_CHANGED_CACHE_PATH.open("w") as f:
                json.dump(
                    _build_lines_changed_cache(11, 5, now=datetime.now(timezone.utc)),
                    f,
                )

            stats = self.make_stats()
            calls = []

            async def fake_query(*args, **kwargs):
                calls.append((args, kwargs))
                return {}

            stats.queries.query = fake_query

            self.assertEqual((11, 5), asyncio.run(_read_lines_changed(stats)))
            self.assertEqual([], calls)

    def test_zero_cache_is_ignored_and_recomputed_from_commit_history(self):
        with temporary_workdir():
            os.makedirs(LINES_CHANGED_CACHE_PATH.parent, exist_ok=True)
            with LINES_CHANGED_CACHE_PATH.open("w") as f:
                json.dump(
                    _build_lines_changed_cache(0, 0, now=datetime.now(timezone.utc)),
                    f,
                )

            stats = self.make_stats()

            async def fake_query(query):
                if 'after: null' in query:
                    return {
                        "data": {
                            "repository": {
                                "defaultBranchRef": {
                                    "target": {
                                        "history": {
                                            "pageInfo": {
                                                "hasNextPage": True,
                                                "endCursor": "cursor-1",
                                            },
                                            "nodes": [
                                                {"additions": 4, "deletions": 1},
                                            ],
                                        }
                                    }
                                }
                            }
                        }
                    }
                if 'after: "cursor-1"' in query:
                    return {
                        "data": {
                            "repository": {
                                "defaultBranchRef": {
                                    "target": {
                                        "history": {
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                            "nodes": [
                                                {"additions": 3, "deletions": 2},
                                            ],
                                        }
                                    }
                                }
                            }
                        }
                    }
                self.fail(f"unexpected query: {query}")

            stats.queries.query = fake_query

            self.assertEqual((7, 3), asyncio.run(_read_lines_changed(stats)))
            with LINES_CHANGED_CACHE_PATH.open("r") as f:
                cached = json.load(f)
            self.assertEqual(7, cached["additions"])
            self.assertEqual(3, cached["deletions"])
            self.assertIn("cached_at", cached)

    def test_stale_positive_cache_is_used_as_fallback(self):
        with temporary_workdir():
            os.makedirs(LINES_CHANGED_CACHE_PATH.parent, exist_ok=True)
            with LINES_CHANGED_CACHE_PATH.open("w") as f:
                json.dump(
                    _build_lines_changed_cache(
                        13,
                        2,
                        now=datetime.now(timezone.utc) - timedelta(days=1),
                    ),
                    f,
                )

            stats = self.make_stats()

            async def fake_query(query):
                return {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "target": None,
                            }
                        }
                    }
                }

            stats.queries.query = fake_query

            self.assertEqual((13, 2), asyncio.run(_read_lines_changed(stats)))

    def test_additional_usernames_are_resolved_to_author_ids(self):
        stats = self.make_stats(additional_usernames={"extra"})

        async def fake_query(query):
            if 'user(login: "extra")' in query:
                return {"data": {"user": {"id": "extra-id"}}}
            if 'author: {id: "viewer-id"}' in query:
                return {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "target": {
                                    "history": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [],
                                    }
                                }
                            }
                        }
                    }
                }
            if 'author: {id: "extra-id"}' in query:
                return {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "target": {
                                    "history": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [
                                            {"additions": 5, "deletions": 1},
                                        ],
                                    }
                                }
                            }
                        }
                    }
                }
            self.fail(f"unexpected query: {query}")

        stats.queries.query = fake_query

        self.assertEqual((5, 1), asyncio.run(_read_lines_changed(stats)))


if __name__ == "__main__":
    unittest.main()
