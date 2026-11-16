import json
import tempfile
import unittest
from pathlib import Path

from authored_languages import (
    CACHE_VERSION,
    FALLBACK_COLORS,
    aggregate_languages,
    commit_signature,
    language_for_path,
    parse_numstat,
    _load_cache,
    _save_cache,
)


class LanguageForPathTests(unittest.TestCase):
    def test_known_extensions(self):
        self.assertEqual("Jupyter Notebook", language_for_path("a/b/lesson.ipynb"))
        self.assertEqual("Rust", language_for_path("src/main.rs"))
        self.assertEqual("TypeScript", language_for_path("web/app.tsx"))
        self.assertEqual("Python", language_for_path("scripts/run.py"))

    def test_extensionless_filenames(self):
        self.assertEqual("Dockerfile", language_for_path("docker/Dockerfile"))
        self.assertEqual("Makefile", language_for_path("Makefile"))
        self.assertEqual("CMake", language_for_path("cmake/CMakeLists.txt"))

    def test_unknown_types_fall_back_to_other(self):
        self.assertEqual("Other", language_for_path("assets/model.weights"))
        self.assertEqual("Other", language_for_path("LICENSE"))


class ParseNumstatTests(unittest.TestCase):
    def test_counts_only_added_lines_by_language(self):
        output = "\n".join(
            [
                "\x01abc123",
                "",
                "10\t2\tsrc/main.rs",
                "3\t0\tsrc/lib.rs",
                "5\t5\tnotebooks/lesson.ipynb",
                "-\t-\tassets/logo.png",
                "\x01def456",
                "",
                "7\t1\tscripts/run.py",
                "",
            ]
        )

        self.assertEqual(
            {
                "Rust": 13,
                "Jupyter Notebook": 5,
                "Python": 7,
            },
            parse_numstat(output),
        )

    def test_ignores_malformed_lines(self):
        self.assertEqual({}, parse_numstat("\x01abc\n\nnot-a-stat-line\n"))


class CommitSignatureTests(unittest.TestCase):
    def test_signature_changes_with_new_commits(self):
        first = commit_signature(["aaa", "bbb"])
        second = commit_signature(["ccc", "aaa", "bbb"])

        self.assertNotEqual(first, second)

    def test_empty_signature(self):
        self.assertEqual("0:", commit_signature([]))


class AggregateLanguagesTests(unittest.TestCase):
    def test_aggregates_and_computes_proportions(self):
        scans = [
            ("owner/one", {"Rust": 300, "Python": 100}),
            ("owner/two", {"Rust": 600}),
        ]

        summary = aggregate_languages(scans, colors={"Rust": "#dea584"})

        self.assertEqual(900, summary["Rust"]["size"])
        self.assertEqual(2, summary["Rust"]["occurrences"])
        self.assertEqual("#dea584", summary["Rust"]["color"])
        self.assertAlmostEqual(90.0, summary["Rust"]["prop"])
        self.assertEqual(10.0, summary["Python"]["prop"])
        self.assertEqual(FALLBACK_COLORS["Python"], summary["Python"]["color"])

    def test_excluded_languages_and_zero_counts_are_dropped(self):
        scans = [("owner/one", {"Rust": 10, "Jupyter Notebook": 5, "Python": 0})]

        summary = aggregate_languages(
            scans, colors={}, excluded_langs={"jupyter notebook"}
        )

        self.assertEqual({"Rust"}, set(summary))

    def test_empty_input(self):
        self.assertEqual({}, aggregate_languages([], colors={}))


class CacheTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.json"
            entries = {
                "owner/repo": {
                    "signature": "2:abc",
                    "languages": {"Rust": 12},
                    "scanned_at": "2026-11-16T00:00:00+00:00",
                }
            }

            _save_cache(path, entries)

            self.assertEqual(entries, _load_cache(path))

    def test_missing_or_invalid_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.json"
            self.assertEqual({}, _load_cache(path))

            with path.open("w") as f:
                json.dump({"version": CACHE_VERSION - 1, "repos": {}}, f)
            self.assertEqual({}, _load_cache(path))

            path.write_text("not json")
            self.assertEqual({}, _load_cache(path))

    def test_cache_file_is_written_as_utf8_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "cache.json"
            _save_cache(path, {"owner/repo": {"languages": {"C++": 1}}})

            self.assertTrue(path.exists())
            with path.open(encoding="utf-8") as f:
                self.assertEqual(CACHE_VERSION, json.load(f)["version"])


if __name__ == "__main__":
    unittest.main()
