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
    def make_stats(self) -> Stats:
        stats = Stats("actor", "token", None, additional_usernames={"extra"})
        stats._repos = {"owner/repo"}
        stats._login = "viewer"
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

            async def fake_query_rest(*args, **kwargs):
                calls.append((args, kwargs))
                return [{"author": {"login": "viewer"}, "weeks": [{"a": 99, "d": 1}]}]

            stats.queries.query_rest = fake_query_rest

            self.assertEqual((11, 5), asyncio.run(_read_lines_changed(stats)))
            self.assertEqual([], calls)

    def test_zero_cache_is_ignored_and_recomputed(self):
        with temporary_workdir():
            os.makedirs(LINES_CHANGED_CACHE_PATH.parent, exist_ok=True)
            with LINES_CHANGED_CACHE_PATH.open("w") as f:
                json.dump(
                    _build_lines_changed_cache(0, 0, now=datetime.now(timezone.utc)),
                    f,
                )

            stats = self.make_stats()

            async def fake_query_rest(*args, **kwargs):
                return [{"author": {"login": "viewer"}, "weeks": [{"a": 7, "d": 3}]}]

            stats.queries.query_rest = fake_query_rest

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

            async def fake_query_rest(*args, **kwargs):
                return {}

            stats.queries.query_rest = fake_query_rest

            self.assertEqual((13, 2), asyncio.run(_read_lines_changed(stats)))


if __name__ == "__main__":
    unittest.main()
