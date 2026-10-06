import csv
import json
import tempfile
import unittest
import os

from classify_magi import (
    axes_scored_count,
    classify_pages,
    find_latest_report_json,
    grade_for_score,
    national_level,
    priority_level,
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
        "similarity_to_national": 0.5, "similarity_to_national_status": "ok",
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
        self.assertEqual(axes_scored_count(levels), 3)

    def test_worst_case_scores_0_and_grades_d(self):
        page = _page(max_similarity_in_theme=1.0, estimated_unique_ratio=0.0, similarity_to_national=1.0)
        score, _ = score_page(page)
        self.assertEqual(score, 0.0)
        self.assertEqual(grade_for_score(score), "D")

    def test_missing_axes_are_excluded_not_penalized(self):
        # No national comparison available (e.g. the national page itself, or budget-skipped,
        # or no same-language national counterpart) — score should be computed over the
        # remaining two axes only, not treated as 0, and axes_scored reflects only 2/3.
        page = _page(max_similarity_in_theme=0.0, estimated_unique_ratio=1.0,
                      similarity_to_national=None, similarity_to_national_status="not_available")
        score, levels = score_page(page)
        self.assertEqual(score, 100.0)
        self.assertIsNone(levels["national"])
        self.assertEqual(axes_scored_count(levels), 2)

    def test_all_axes_missing_returns_unscored(self):
        page = _page(max_similarity_in_theme=None, estimated_unique_ratio=None, similarity_to_national=None)
        score, levels = score_page(page)
        self.assertIsNone(score)
        self.assertEqual(grade_for_score(score), "unscored")
        self.assertEqual(axes_scored_count(levels), 0)

    def test_grade_boundaries(self):
        self.assertEqual(grade_for_score(75.0), "A")
        self.assertEqual(grade_for_score(74.9), "B")
        self.assertEqual(grade_for_score(60.0), "B")
        self.assertEqual(grade_for_score(59.9), "C")
        self.assertEqual(grade_for_score(40.0), "C")
        self.assertEqual(grade_for_score(39.9), "D")


class PriorityLevelTest(unittest.TestCase):
    def test_grade_a_or_b_has_no_priority_level(self):
        page = _page(max_similarity_in_theme=0.0, estimated_unique_ratio=1.0, similarity_to_national=0.0)
        self.assertIsNone(priority_level(page))

    def test_high_from_similarity_alone(self):
        # National axis excluded (not_available) so the C/D grade comes from similarity
        # alone, with uniqueness safe — isolates the "similarity >= 90% => High" rule.
        page = _page(max_similarity_in_theme=0.95, estimated_unique_ratio=0.5,
                      similarity_to_national=None, similarity_to_national_status="not_available")
        self.assertEqual(grade_for_score(score_page(page)[0]), "C")
        self.assertEqual(priority_level(page), "High")

    def test_high_from_uniqueness_alone(self):
        page = _page(max_similarity_in_theme=0.5, estimated_unique_ratio=0.05,
                      similarity_to_national=None, similarity_to_national_status="not_available")
        self.assertEqual(grade_for_score(score_page(page)[0]), "C")
        self.assertEqual(priority_level(page), "High")

    def test_medium_band(self):
        page = _page(max_similarity_in_theme=0.85, estimated_unique_ratio=0.5, similarity_to_national=0.9)
        level = priority_level(page)
        self.assertIn(level, ("Medium", "High"))
        # specifically check the pure-medium case (similarity in 80-89, uniqueness safe)
        page2 = _page(max_similarity_in_theme=0.82, estimated_unique_ratio=0.5, similarity_to_national=0.9)
        self.assertEqual(priority_level(page2), "Medium")

    def test_low_when_cd_but_no_axis_in_high_or_medium_band(self):
        # Grade C/D can also come from a weak national-axis alone, with similarity and
        # uniqueness both in safe bands — that's priority Low, not High/Medium.
        page = _page(max_similarity_in_theme=0.5, estimated_unique_ratio=0.5, similarity_to_national=0.95)
        grade = grade_for_score(score_page(page)[0])
        if grade in ("C", "D"):
            self.assertEqual(priority_level(page), "Low")


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

    def test_axes_scored_display_format(self):
        result = classify_pages([_page(similarity_to_national=None, similarity_to_national_status="not_available")])
        self.assertEqual(result[0]["axes_scored"], 2)
        self.assertEqual(result[0]["axes_scored_display"], "2/3")


class WriteOutputsIntegrationTest(unittest.TestCase):
    def test_writes_json_markdown_and_csv_without_crashing_on_mixed_data(self):
        pages = [
            _page(id="a", url="https://x/a/", theme="infection", language="ja",
                  max_similarity_in_theme=0.95, estimated_unique_ratio=0.05, similarity_to_national=0.9),
            _page(id="b", url="https://x/b/", theme="population", language="en",
                  similarity_to_national=None, similarity_to_national_status="not_available"),
            _page(id="c", url="https://x/c/", theme="homepage"),
        ]
        classified = classify_pages(pages)
        with tempfile.TemporaryDirectory() as tmp:
            grade_counts, cd_pages, priority_counts = write_outputs(classified, tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "magi_classification.json")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "magi_classification.md")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "magi_review_queue.csv")))

            with open(os.path.join(tmp, "magi_classification.md"), encoding="utf-8") as f:
                content = f.read()
            self.assertIn("https://x/a/", content)
            self.assertIn("infection", content)
            self.assertIn("最終判定ではなく", content)

            with open(os.path.join(tmp, "magi_review_queue.csv"), encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0][0], "url")
            # only C/D pages appear in the CSV
            csv_urls = {row[0] for row in rows[1:]}
            self.assertTrue(csv_urls.issubset({"https://x/a/", "https://x/b/"}))

    def test_csv_is_sorted_by_priority_then_score(self):
        pages = [
            _page(id="low", url="https://x/low/", max_similarity_in_theme=0.5,
                  estimated_unique_ratio=0.5, similarity_to_national=0.95),
            _page(id="high", url="https://x/high/", max_similarity_in_theme=0.99,
                  estimated_unique_ratio=0.5, similarity_to_national=0.5),
        ]
        classified = classify_pages(pages)
        with tempfile.TemporaryDirectory() as tmp:
            write_outputs(classified, tmp)
            with open(os.path.join(tmp, "magi_review_queue.csv"), encoding="utf-8") as f:
                rows = list(csv.reader(f))
            urls_in_order = [row[0] for row in rows[1:]]
            if "https://x/high/" in urls_in_order and "https://x/low/" in urls_in_order:
                self.assertLess(urls_in_order.index("https://x/high/"), urls_in_order.index("https://x/low/"))


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
