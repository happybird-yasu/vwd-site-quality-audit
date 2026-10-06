import json
import unittest

from run_audit import (
    build_sample,
    classify_links,
    classify_url,
    compute_similarity,
    extract_main_text,
    find_dataset_jsonld,
    homepage_item,
    parse_page,
    parse_sitemap_xml,
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


class SimilarityTest(unittest.TestCase):
    def _page(self, id_, theme, region, language, text):
        return {"id": id_, "theme": theme, "region": region, "language": language, "_text": text}

    def test_national_comparison_only_matches_same_language(self):
        pages = [
            self._page("ja-national", "population", "national", "ja", "全国の人口統計データです。 " * 10),
            self._page("ja-kanto", "population", "kanto", "ja", "全国の人口統計データです。関東地方は特に多い。 " * 10),
            self._page("en-national", "population", "national", "en", "National population statistics for Japan. " * 10),
            self._page("en-kanto", "population", "kanto", "en", "National population statistics for Japan. Kanto region is dense. " * 10),
        ]
        result = compute_similarity(pages)
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
        result = compute_similarity(pages)
        national = next(p for p in result if p["id"] == "ja-national")
        self.assertIsNone(national["similarity_to_national"])


class HomepageItemTest(unittest.TestCase):
    def test_homepage_item_shape(self):
        item = homepage_item()
        self.assertEqual(item["url"], "https://app-navi.biz/")
        self.assertEqual(item["theme"], "homepage")

    def test_homepage_does_not_crash_similarity_with_no_peers(self):
        pages = [
            {**homepage_item(), "_text": "Visible World by Data homepage text."},
        ]
        result = compute_similarity(pages)
        self.assertEqual(result[0]["theme_peer_count"], 0)
        self.assertEqual(result[0]["max_similarity_in_theme"], 0.0)
        self.assertIsNone(result[0]["similarity_to_national"])


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
