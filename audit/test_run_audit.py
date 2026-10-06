import json
import tempfile
import time
import unittest
from unittest import mock

import run_audit
from run_audit import (
    build_sample,
    classify_links,
    classify_url,
    compute_similarity,
    crawl_pages,
    extract_main_text,
    find_dataset_jsonld,
    homepage_item,
    parse_page,
    parse_sitemap_xml,
    select_pages,
    summarize,
    write_report,
)
from bs4 import BeautifulSoup

SAMPLE_HTML = """
<html><body>
<header><nav><a href="/">Home</a><a href="/about/">About</a></nav></header>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Dataset","name":"Population map","description":"Population by region","license":"https://example.invalid/license"}
</script>
<article>
  <h1>Population of Kanto</h1>
  <p>This is the unique body text about Kanto population trends, written specifically for this page.</p>
  <a href="/population-national/">See national data</a>
  <a href="https://www.e-stat.go.jp/">Source: e-Stat</a>
</article>
<footer class="vwd-article-footer"><a href="/onsen-map-national/">Related</a></footer>
</body></html>
"""


class DatasetExtractionOrderTest(unittest.TestCase):
    def test_dataset_found_even_though_main_text_removes_script_tags(self):
        # Regression test: extract_main_text() decompose()s <script> tags in
        # place. If find_dataset_jsonld() were called on that same soup AFTER
        # extract_main_text(), the JSON-LD would already be gone and this
        # would wrongly report no Dataset. parse_page() must read the
        # dataset before mutating the tree.
        result = parse_page(SAMPLE_HTML, "https://app-navi.biz/population-kanto/")
        self.assertTrue(result["dataset_jsonld_found"])
        self.assertEqual(len(result["dataset_entries"]), 1)
        entry = result["dataset_entries"][0]
        self.assertTrue(entry["has_description"])
        self.assertTrue(entry["has_license"])
        self.assertTrue(entry["has_name"])

    def test_wrong_order_would_have_missed_it(self):
        # Documents the bug this test file guards against: calling
        # find_dataset_jsonld() on a soup that extract_main_text() already
        # mutated returns nothing, even though the JSON-LD was present in
        # the original HTML.
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        extract_main_text(soup)
        datasets_after_mutation = find_dataset_jsonld(soup)
        self.assertEqual(datasets_after_mutation, [])


class LinkClassificationTest(unittest.TestCase):
    def test_body_links_exclude_nav_and_footer(self):
        result = parse_page(SAMPLE_HTML, "https://app-navi.biz/population-kanto/")
        # body scope: only the <article> content's 2 links (1 internal, 1 external)
        self.assertEqual(result["body_internal_links"], 1)
        self.assertEqual(result["body_external_links"], 1)
        # page scope: nav (2 internal) + article (1 internal, 1 external) + footer (1 internal)
        self.assertEqual(result["page_internal_links"], 4)
        self.assertEqual(result["page_external_links"], 1)

    def test_classify_links_basic(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        internal, external, samples = classify_links(soup, "https://app-navi.biz/x/")
        self.assertGreaterEqual(internal, 1)
        self.assertGreaterEqual(external, 1)
        self.assertTrue(any("e-stat.go.jp" in s for s in samples))


class MainTextExtractionTest(unittest.TestCase):
    def test_removes_boilerplate_footer_and_nav(self):
        text = extract_main_text(BeautifulSoup(SAMPLE_HTML, "html.parser"))
        self.assertIn("unique body text about Kanto", text)
        self.assertNotIn("Related", text)
        self.assertNotIn("Home", text)


class BuildSampleTest(unittest.TestCase):
    def test_balances_across_themes_and_regions(self):
        items = []
        for theme in ["population", "onsen"]:
            for region in ["national", "kanto", "kinki", "kyushu"]:
                for lang in ["ja", "en"]:
                    items.append({
                        "id": f"{lang}--{theme}-{region}",
                        "theme": theme,
                        "region": region,
                        "language": lang,
                        "url": f"https://example.invalid/{theme}-{region}-{lang}/",
                    })
        sample = build_sample(items, 8)
        self.assertLessEqual(len(sample), 8)
        themes_in_sample = {s["theme"] for s in sample}
        self.assertEqual(themes_in_sample, {"population", "onsen"})

    def test_is_deterministic_given_fixed_seed(self):
        items = [
            {"id": f"ja--t-{i}", "theme": "t", "region": "national" if i == 0 else f"r{i}",
             "language": "ja", "url": f"https://example.invalid/{i}/"}
            for i in range(10)
        ]
        first = [s["id"] for s in build_sample(items, 5)]
        second = [s["id"] for s in build_sample(items, 5)]
        self.assertEqual(first, second)

    def test_every_theme_gets_at_least_one_pick_before_any_gets_a_second(self):
        # Regression test: the pilot run with the old "fill then break" logic
        # dropped 5 of 12 themes entirely because earlier themes (sorted
        # alphabetically) already used up the whole sample_size budget.
        themes = ["airport", "hotel_ryokan", "infection", "medical", "minerals",
                  "onsen", "population", "port", "road", "schools", "station", "travel"]
        items = []
        for theme in themes:
            for region in ["national", "kanto", "kinki", "kyushu", "tohoku"]:
                items.append({
                    "id": f"{theme}-{region}", "theme": theme, "region": region,
                    "language": "ja", "url": f"https://example.invalid/{theme}-{region}/",
                })
        sample = build_sample(items, 26)
        themes_in_sample = {s["theme"] for s in sample}
        self.assertEqual(themes_in_sample, set(themes))

    def test_fewer_slots_than_themes_still_covers_as_many_as_possible(self):
        items = [
            {"id": f"{theme}-national", "theme": theme, "region": "national",
             "language": "ja", "url": f"https://example.invalid/{theme}/"}
            for theme in ["a", "b", "c", "d", "e"]
        ]
        sample = build_sample(items, 3)
        self.assertEqual(len(sample), 3)
        self.assertEqual(len({s["theme"] for s in sample}), 3)


class ClassifyUrlTest(unittest.TestCase):
    def test_national_japanese(self):
        c = classify_url("https://app-navi.biz/population-national/")
        self.assertEqual(c["theme"], "population")
        self.assertEqual(c["region"], "national")
        self.assertEqual(c["language"], "ja")

    def test_regional_with_composite_region_keyword(self):
        c = classify_url("https://app-navi.biz/school-count-map-chubu-hokuriku/")
        self.assertEqual(c["theme"], "schools")
        self.assertEqual(c["region"], "chubu-hokuriku")
        self.assertEqual(c["language"], "ja")

    def test_translated_page_via_path_prefix(self):
        c = classify_url("https://app-navi.biz/de/onsen-map-kanto-de/")
        self.assertEqual(c["theme"], "onsen")
        self.assertEqual(c["region"], "kanto")
        self.assertEqual(c["language"], "de")

    def test_translated_page_via_slug_suffix_only(self):
        c = classify_url("https://app-navi.biz/hotel-ryokan-tohoku-fr/")
        self.assertEqual(c["theme"], "hotel_ryokan")
        self.assertEqual(c["region"], "tohoku")
        self.assertEqual(c["language"], "fr")

    def test_infection_weekly_is_always_national(self):
        c = classify_url("https://app-navi.biz/infection-weekly-2026-w35-es/")
        self.assertEqual(c["theme"], "infection")
        self.assertEqual(c["region"], "national")
        self.assertEqual(c["language"], "es")

    def test_unknown_theme_for_unrelated_page(self):
        c = classify_url("https://app-navi.biz/privacy-policy/")
        self.assertEqual(c["theme"], "unknown")

    def test_aging_rate_map_is_classified_not_unknown(self):
        c = classify_url("https://app-navi.biz/aging-rate-map-national/")
        self.assertEqual(c["theme"], "aging_rate")
        self.assertEqual(c["region"], "national")
        self.assertEqual(c["language"], "ja")

        c = classify_url("https://app-navi.biz/en/aging-rate-map-kanto-en/")
        self.assertEqual(c["theme"], "aging_rate")
        self.assertEqual(c["region"], "kanto")
        self.assertEqual(c["language"], "en")

    def test_station_passenger_ranking_is_classified_not_unknown(self):
        c = classify_url("https://app-navi.biz/en/station-passenger-ranking-national-en/")
        self.assertEqual(c["theme"], "station_ranking")
        self.assertEqual(c["region"], "national")
        self.assertEqual(c["language"], "en")

        c = classify_url("https://app-navi.biz/en/station-passenger-ranking-kanto-en/")
        self.assertEqual(c["theme"], "station_ranking")
        self.assertEqual(c["region"], "kanto")
        self.assertEqual(c["language"], "en")


class SimilarityTest(unittest.TestCase):
    def _page(self, id_, theme, region, language, text):
        return {"id": id_, "theme": theme, "region": region, "language": language, "_text": text}

    def test_large_theme_group_stays_fast(self):
        # Regression test for the run that spent 38+ minutes stuck in this
        # function with zero output and was killed by the CI job timeout
        # before writing any report. A naive all-pairs comparison here is
        # O(n^2) in page count AND O(n^2) in text length for realistic
        # (highly repetitive) article text, difflib's worst case. 80 pages
        # in one theme with repetitive ~1500-char bodies must still finish
        # in well under the old per-run timeout.
        boilerplate = "このテーマの定型文です。出典は公式統計です。地域ごとに比較できます。" * 20
        pages = [
            self._page(f"p{i}", "population", "kanto" if i else "national", "ja",
                       boilerplate + f" 固有コメント{i}")
            for i in range(80)
        ]
        start = time.time()
        result, budget_exceeded = compute_similarity(pages)
        elapsed = time.time() - start
        self.assertFalse(budget_exceeded)
        self.assertLess(elapsed, 15, f"took {elapsed:.1f}s — comparison bounding regressed")
        for p in result:
            self.assertLessEqual(p["theme_peer_count"], 79)

    def test_time_budget_exceeded_still_returns_all_pages_with_defaults(self):
        pages = [self._page(f"p{i}", "population", "kanto", "ja", "text " * 50) for i in range(5)]
        with mock.patch.object(run_audit, "SIMILARITY_TIME_BUDGET_SECONDS", -1):
            result, budget_exceeded = compute_similarity(pages)
        self.assertTrue(budget_exceeded)
        self.assertEqual(len(result), 5)
        for p in result:
            self.assertEqual(p["max_similarity_in_theme"], 0.0)
            self.assertIsNone(p["similarity_to_national"])

    def test_national_comparison_only_matches_same_language(self):
        pages = [
            self._page("ja-national", "population", "national", "ja", "全国の人口統計データです。 " * 10),
            self._page("ja-kanto", "population", "kanto", "ja", "全国の人口統計データです。関東地方は特に多い。 " * 10),
            self._page("en-national", "population", "national", "en", "National population statistics for Japan. " * 10),
            self._page("en-kanto", "population", "kanto", "en", "National population statistics for Japan. Kanto region is dense. " * 10),
        ]
        result, budget_exceeded = compute_similarity(pages)
        self.assertFalse(budget_exceeded)
        by_id = {p["id"]: p for p in result}

        # ja-kanto must compare against ja-national, not en-national
        self.assertIsNotNone(by_id["ja-kanto"]["similarity_to_national"])
        # en-kanto must compare against en-national, not ja-national
        self.assertIsNotNone(by_id["en-kanto"]["similarity_to_national"])
        # a Japanese-only and an English-only text share almost nothing in common,
        # so if the language filter were broken this would collapse near 0
        self.assertGreater(by_id["ja-kanto"]["similarity_to_national"], 0.3)
        self.assertGreater(by_id["en-kanto"]["similarity_to_national"], 0.3)

    def test_national_page_itself_has_no_national_comparison(self):
        pages = [
            self._page("ja-national", "population", "national", "ja", "text"),
            self._page("ja-kanto", "population", "kanto", "ja", "text"),
        ]
        result, _ = compute_similarity(pages)
        national = next(p for p in result if p["id"] == "ja-national")
        self.assertIsNone(national["similarity_to_national"])
        self.assertEqual(national["similarity_to_national_status"], "not_applicable")

    def test_cross_language_siblings_are_excluded_from_theme_similarity(self):
        # A ja page and its en translation share the same theme but must never be
        # compared against each other: that's an intentional 1:1 translation, not
        # duplicate/templated content, and the ratio would be meaningless anyway.
        pages = [
            self._page("ja-kanto", "population", "kanto", "ja", "全国の人口統計データです。関東地方は特に多い。" * 10),
            self._page("en-kanto", "population", "kanto", "en", "National population statistics. Kanto region is dense." * 10),
        ]
        result, _ = compute_similarity(pages)
        for p in result:
            self.assertEqual(p["theme_peer_count"], 0)
            self.assertEqual(p["max_similarity_in_theme"], 0.0)
            self.assertIsNone(p["most_similar_peer_id"])

    def test_national_comparison_status_not_available_when_no_same_language_national_page(self):
        pages = [
            self._page("en-kanto", "population", "kanto", "en", "text"),
            self._page("ja-national", "population", "national", "ja", "text"),
        ]
        result, _ = compute_similarity(pages)
        en_kanto = next(p for p in result if p["id"] == "en-kanto")
        self.assertIsNone(en_kanto["similarity_to_national"])
        self.assertEqual(en_kanto["similarity_to_national_status"], "not_available")

    def test_national_comparison_status_ok_when_same_language_national_page_exists(self):
        pages = [
            self._page("ja-kanto", "population", "kanto", "ja", "text about kanto" * 5),
            self._page("ja-national", "population", "national", "ja", "text about japan" * 5),
        ]
        result, _ = compute_similarity(pages)
        ja_kanto = next(p for p in result if p["id"] == "ja-kanto")
        self.assertEqual(ja_kanto["similarity_to_national_status"], "ok")
        self.assertIsNotNone(ja_kanto["similarity_to_national"])


class HomepageItemTest(unittest.TestCase):
    def test_homepage_item_shape(self):
        item = homepage_item()
        self.assertEqual(item["url"], "https://app-navi.biz/")
        self.assertEqual(item["theme"], "homepage")

    def test_homepage_does_not_crash_similarity_with_no_peers(self):
        pages = [
            {**homepage_item(), "_text": "Visible World by Data homepage text."},
        ]
        result, _ = compute_similarity(pages)
        self.assertEqual(result[0]["theme_peer_count"], 0)
        self.assertEqual(result[0]["max_similarity_in_theme"], 0.0)
        self.assertIsNone(result[0]["similarity_to_national"])


class SelectPagesTest(unittest.TestCase):
    def test_full_mode_returns_every_url_plus_homepage_including_unknown_theme(self):
        classified = [
            {"id": "a", "theme": "population", "region": "national", "language": "ja", "url": "https://x/a/"},
            {"id": "b", "theme": "onsen", "region": "kanto", "language": "ja", "url": "https://x/b/"},
            {"id": "c", "theme": "unknown", "region": "other", "language": "ja", "url": "https://x/c/"},
        ]
        result = select_pages(classified, full=True, sample_size=26)
        self.assertEqual(len(result), 4)  # 3 classified + homepage
        self.assertIn(homepage_item()["id"], [r["id"] for r in result])
        self.assertIn("c", [r["id"] for r in result])  # unknown-theme page kept in full mode

    def test_sample_mode_still_caps_at_sample_size_plus_homepage(self):
        classified = [
            {"id": f"t{i}-national", "theme": f"t{i}", "region": "national", "language": "ja",
             "url": f"https://x/t{i}/"}
            for i in range(20)
        ]
        result = select_pages(classified, full=False, sample_size=5)
        self.assertEqual(len(result), 6)  # 5 sampled + homepage


class CrawlPagesTimeBudgetTest(unittest.TestCase):
    def test_stops_and_marks_remainder_skipped_when_budget_already_exceeded(self):
        # Regression test for the run that hung for an hour and was killed by the
        # GitHub Actions job timeout with nothing saved. A negative budget forces
        # the very first iteration to trip the check, so the whole sample is
        # skipped instead of fetched — proving the early-exit path writes a
        # usable (if empty) result rather than running unbounded.
        sample = [
            {"id": "a", "url": "https://x/a/", "theme": "population", "region": "national", "language": "ja"},
            {"id": "b", "url": "https://x/b/", "theme": "population", "region": "kanto", "language": "ja"},
        ]
        with mock.patch.object(run_audit, "fetch_with_retry") as fetch_mock:
            pages, failures, budget_exceeded = crawl_pages(sample, disallow_prefixes=set(), max_seconds=-1)

        fetch_mock.assert_not_called()
        self.assertTrue(budget_exceeded)
        self.assertEqual(pages, [])
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(f["reason"] == "skipped_time_budget_exceeded" for f in failures))

    def test_no_budget_means_unbounded(self):
        sample = [{"id": "a", "url": "https://x/a/", "theme": "population", "region": "national", "language": "ja"}]
        fake_resp = mock.Mock(status_code=200, text="<html><body><article>hi</article></body></html>")
        with mock.patch.object(run_audit, "fetch_with_retry", return_value=(fake_resp, None)), \
             mock.patch.object(run_audit.time, "sleep"):
            pages, failures, budget_exceeded = crawl_pages(sample, disallow_prefixes=set(), max_seconds=None)

        self.assertFalse(budget_exceeded)
        self.assertEqual(len(pages), 1)
        self.assertEqual(failures, [])


class SummarizeAndReportIntegrationTest(unittest.TestCase):
    def test_handles_pages_with_none_similarity_fields_from_a_budget_cutoff(self):
        # Regression test for the real full-crawl run: compute_similarity's time
        # budget left the tail of pages with estimated_unique_ratio=None /
        # similarity_to_national=None (by design — "not compared"), and
        # summarize()'s flagged-page filter did `None < 0.3`, crashing with
        # TypeError after a 21-minute crawl had already succeeded, so nothing
        # was ever written. summarize() and write_report() must both tolerate
        # this partial-data shape all the way through.
        pages = []
        for i in range(5):
            pages.append({
                "id": f"compared-{i}", "theme": "population", "region": "kanto", "language": "ja",
                "url": f"https://x/{i}/", "char_count": 500, "dataset_jsonld_found": False,
                "dataset_entries": [], "max_similarity_in_theme": 0.7, "estimated_unique_ratio": 0.2,
                "similarity_to_national": 0.5,
                "page_internal_links": 1, "page_external_links": 0,
                "body_internal_links": 1, "body_external_links": 0,
            })
        for i in range(5):
            pages.append({
                "id": f"uncompared-{i}", "theme": "population", "region": "kyushu", "language": "ja",
                "url": f"https://x/u{i}/", "char_count": 500, "dataset_jsonld_found": False,
                "dataset_entries": [], "max_similarity_in_theme": 0.0, "estimated_unique_ratio": None,
                "similarity_to_national": None,
                "page_internal_links": 1, "page_external_links": 0,
                "body_internal_links": 1, "body_external_links": 0,
            })

        summary = summarize(pages, failures=[], sitemap_url_count=10)
        self.assertEqual(summary["fetched_ok"], 10)

        with tempfile.TemporaryDirectory() as tmp:
            write_report(pages, [], summary, {"selected": []}, tmp, full=True)
            with open(f"{tmp}/report.md", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("https://x/u0/", content)


class SitemapParsingTest(unittest.TestCase):
    def test_urlset_returns_locs(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://app-navi.biz/population-national/</loc></url>
  <url><loc>https://app-navi.biz/population-kanto/</loc></url>
</urlset>"""
        kind, locs = parse_sitemap_xml(xml)
        self.assertEqual(kind, "urlset")
        self.assertEqual(len(locs), 2)
        self.assertIn("https://app-navi.biz/population-national/", locs)

    def test_sitemapindex_returns_child_sitemap_locs(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://app-navi.biz/post-sitemap1.xml</loc></sitemap>
  <sitemap><loc>https://app-navi.biz/post-sitemap2.xml</loc></sitemap>
</sitemapindex>"""
        kind, locs = parse_sitemap_xml(xml)
        self.assertEqual(kind, "sitemapindex")
        self.assertEqual(locs, [
            "https://app-navi.biz/post-sitemap1.xml",
            "https://app-navi.biz/post-sitemap2.xml",
        ])


if __name__ == "__main__":
    unittest.main()
