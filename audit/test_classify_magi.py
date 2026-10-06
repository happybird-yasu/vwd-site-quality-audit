import json
import tempfile
import unittest
import os

from classify_magi import (
    classify_pages,
    find_latest_report_json,
    grade_for_score,
    national_level,
    similarity_level,
    score_page,
    uniqueness_level,
    write_outputs,
)


def _page(**overrides):
    base = {
        "id": "https://x/p/", "url": "https://x/p/", "theme": "population",
        "region": "kanto", "language": "ja",
        "max_similarity_in_theme": 0.5, "estimated_unique_ratio": 0.5,
        "similarity_to_national": 0.5,
    }
    base.update(overrides)
    return base


class LevelBandingTest(unittest.TestCase):
    def test_similarity_level_lower_is_safer(self):
        self.assertEqual(similarity_level(0.0), 3)
        self.assertEqual(similarity_level(0.69), 3)
        self.assertEqual(similarity_level(0.70), 2)
        self.assertEqual(similarity_level(0.79), 2)
        self.assertEqual(similarity_level(0.80), 1)
        self.assertEqual(similarity_level(0.89), 1)
        self.assertEqual(similarity_level(0.90), 0)
        self.assertEqual(similarity_level(1.0), 0)
        self.assertIsNone(similarity_level(None))

    def test_uniqueness_level_higher_is_safer(self):
        self.assertEqual(uniqueness_level(1.0), 3)
        self.assertEqual(uniqueness_level(0.40), 3)
        self.assertEqual(uniqueness_level(0.25), 2)
        self.assertEqual(uniqueness_level(0.39), 2)
        self.assertEqual(uniqueness_level(0.15), 1)
        self.assertEqual(uniqueness_level(0.24), 1)
        self.assertEqual(uniqueness_level(0.0), 0)
        self.assertEqual(uniqueness_level(0.14), 0)
        self.assertIsNone(uniqueness_level(None))

    def test_national_level_lower_is_safer(self):
        self.assertEqual(national_level(0.0), 3)
        self.assertEqual(national_level(0.64), 3)
        self.assertEqual(national_level(0.65), 2)
        self.assertEqual(national_level(0.74), 2)
        self.assertEqual(national_level(0.75), 1)
        self.assertEqual(national_level(0.84), 1)
        self.assertEqual(national_level(0.85), 0)
        self.assertIsNone(national_level(None))


class ScorePageTest(unittest.TestCase):
    def test_best_case_scores_100_and_grades_a(self):
        page = _page(max_similarity_in_theme=0.0, estimated_unique_ratio=1.0, similarity_to_national=0.0)
        score, levels = score_page(page)
        self.assertEqual(score, 100.0)
        self.assertEqual(levels, {"similarity": 3, "uniqueness": 3, "national": 3})
        self.assertEqual(grade_for_score(score), "A")

    def test_worst_case_scores_0_and_grades_d(self):
        page = _page(max_similarity_in_theme=1.0, estimated_unique_ratio=0.0, similarity_to_national=1.0)
        score, _ = score_page(page)
        self.assertEqual(score, 0.0)
        self.assertEqual(grade_for_score(score), "D")

    def test_missing_axes_are_excluded_not_penalized(self):
        # No national comparison available (e.g. the national page itself, or budget-skipped) —
        # score should be computed over the remaining two axes only, not treated as 0.
        page = _page(max_similarity_in_theme=0.0, estimated_unique_ratio=1.0, similarity_to_national=None)
        score, levels = score_page(page)
        self.assertEqual(score, 100.0)
        self.assertIsNone(levels["national"])

    def test_all_axes_missing_returns_unscored(self):
        page = _page(max_similarity_in_theme=None, estimated_unique_ratio=None, similarity_to_national=None)
        score, _ = score_page(page)
        self.assertIsNone(score)
        self.assertEqual(grade_for_score(score), "unscored")

    def test_grade_boundaries(self):
        self.assertEqual(grade_for_score(75.0), "A")
        self.assertEqual(grade_for_score(74.9), "B")
        self.assertEqual(grade_for_score(60.0), "B")
        self.assertEqual(grade_for_score(59.9), "C")
        self.assertEqual(grade_for_score(40.0), "C")
        self.assertEqual(grade_for_score(39.9), "D")


class ClassifyPagesTest(unittest.TestCase):
    def test_homepage_is_excluded(self):
        pages = [_page(theme="homepage"), _page()]
        result = classify_pages(pages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["theme"], "population")

    def test_every_page_flags_region_specificity_as_pending(self):
        result = classify_pages([_page()])
        self.assertTrue(result[0]["region_specificity_pending"])

    def test_priority_themes_flagged(self):
        result = classify_pages([_page(theme="infection"), _page(theme="onsen")])
        by_theme = {r["theme"]: r for r in result}
        self.assertTrue(by_theme["infection"]["priority_review"])
        self.assertFalse(by_theme["onsen"]["priority_review"])


class WriteOutputsIntegrationTest(unittest.TestCase):
    def test_writes_json_and_markdown_without_crashing_on_mixed_data(self):
        pages = [
            _page(id="a", url="https://x/a/", theme="infection", max_similarity_in_theme=0.95,
                  estimated_unique_ratio=0.05, similarity_to_national=0.9),
            _page(id="b", url="https://x/b/", theme="population", similarity_to_national=None),
            _page(id="c", url="https://x/c/", theme="homepage"),
        ]
        classified = classify_pages(pages)
        with tempfile.TemporaryDirectory() as tmp:
            write_outputs(classified, tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "magi_classification.json")))
            with open(os.path.join(tmp, "magi_classification.md"), encoding="utf-8") as f:
                content = f.read()
            self.assertIn("https://x/a/", content)
            self.assertIn("infection", content)


class FindLatestReportJsonTest(unittest.TestCase):
    def test_finds_most_recent_run_by_sorted_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            for run_id in ("run-20260101T000000Z", "run-20260102T000000Z"):
                d = os.path.join(tmp, run_id)
                os.makedirs(d)
                with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
                    json.dump({"pages": []}, f)
            latest = find_latest_report_json(tmp)
            self.assertIn("20260102", latest)

    def test_returns_none_when_no_runs_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_latest_report_json(tmp))


if __name__ == "__main__":
    unittest.main()
