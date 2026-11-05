import os
import random
import shutil
import tempfile
import unittest

import allpairs as ap
import build_snapshot as bs


def rec(url, text, lang="ja", theme="t1"):
    return {"url": url, "language": lang, "theme": theme, "text": text, "text_sha256": ap.sha256(text)}


def rnd_text(seed, n):
    r = random.Random(seed)
    return "".join(r.choice("あいうえおかきくけこさしすせそたちつてと") for _ in range(n))


def corpus():
    base = rnd_text(1, 2500)
    return [
        rec("https://x/a", base),
        rec("https://x/b", base[:1500] + rnd_text(2, 1000)),       # same head, different tail
        rec("https://x/c", rnd_text(3, 1500) + base[1500:]),        # different head, same tail
        rec("https://x/d", rnd_text(4, 2500)),
        rec("https://x/e", base, lang="en"),
        rec("https://x/f", rnd_text(5, 1800), theme="t2"),
    ]


class T(unittest.TestCase):
    def run_all(self, recs, shards, d):
        for s in range(shards):
            ap.run_shard(recs, s, shards, d)
        return ap.collect_results([d])[0]

    def test_beyond_1500_duplicate_detected(self):
        a = rnd_text(9, 1500)
        shared = rnd_text(10, 3000)
        x, y = rec("https://x/1", a + shared), rec("https://x/2", rnd_text(11, 1500) + shared)
        r = ap.compare_pair("p", *sorted([x, y], key=lambda r: r["url"]))
        self.assertGreater(r["sym"], 0.5)  # old 1500-char cap would see ~0
        old = ap.compare_texts(x["text"][:1500], y["text"][:1500])[0]
        self.assertLess(old, 0.2)

    def test_identical_tail_not_unique(self):
        recs = corpus()
        p = {r["url"]: r for r in recs}
        r = ap.compare_pair("p", p["https://x/a"], p["https://x/c"])
        self.assertGreaterEqual(r["matched_ab"], 1000)

    def test_symmetry_and_canonical_order(self):
        recs = corpus()
        pa = {pid: (a, b) for pid, a, b in ap.expected_pairs(recs)}
        for pid, (a, b) in pa.items():
            r1 = ap.compare_pair(pid, a, b)
            # swapped inputs through the canonical path give identical output
            pid2 = ap.pair_id(b["url"], a["url"])
            self.assertEqual(pid, pid2)
            r2 = ap.compare_texts(b["text"], a["text"])
            self.assertAlmostEqual(r1["sym"], round((r2[0] + r2[1]) / 2, 6), places=5)

    def test_input_order_and_shard_count_invariant(self):
        recs = corpus()
        res = {}
        for shards, order in ((1, recs), (3, list(reversed(recs))), (5, random.Random(0).sample(recs, len(recs)))):
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d)
            res[shards] = {r["pair_id"]: r for r in self.run_all(order, shards, d)}
        self.assertEqual(res[1], res[3])
        self.assertEqual(res[1], res[5])
        self.assertEqual(len(res[1]), 15)

    def test_resume_equals_full(self):
        recs = corpus()
        full, part = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, full)
        self.addCleanup(shutil.rmtree, part)
        a = {r["pair_id"]: r for r in self.run_all(recs, 2, full)}
        # interrupted: budget 0 sec -> nothing (or little) done, then resume
        for s in range(2):
            ap.run_shard(recs, s, 2, part, max_seconds=0)
        for s in range(2):
            ap.run_shard(recs, s, 2, part)
        b = {r["pair_id"]: r for r in ap.collect_results([part])[0]}
        self.assertEqual(a, b)

    def test_resume_from_other_dir_no_duplicates(self):
        recs = corpus()
        d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d1)
        self.addCleanup(shutil.rmtree, d2)
        ap.run_shard(recs, 0, 1, d1)
        st = ap.run_shard(recs, 0, 1, d2, resume_dirs=[d1])
        self.assertEqual(st["compared_now"], 0)
        res, _, conf, info = ap.collect_results([d2])
        self.assertEqual(len(res), 15)
        self.assertEqual(conf, [])
        self.assertEqual(info["identical_duplicates"], 0)

    def test_missing_and_duplicate_and_extra_detected(self):
        recs = corpus()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        res = self.run_all(recs, 1, d)
        self.assertTrue(ap.verify_completeness(recs, res)["exact_match"])
        v = ap.verify_completeness(recs, res[1:])
        self.assertEqual(v["missing"], 1)
        self.assertFalse(v["exact_match"])
        v = ap.verify_completeness(recs, res + [res[0]])
        self.assertEqual(v["duplicates"], 1)
        self.assertFalse(v["exact_match"])
        v = ap.verify_completeness(recs, res + [dict(res[0], pair_id="deadbeef")])
        self.assertEqual(v["extra"], 1)
        self.assertFalse(v["exact_match"])

    def test_errors_not_counted_as_pass(self):
        recs = corpus()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)

        def bad(pid, a, b):
            if b["url"].endswith("/d"):
                raise RuntimeError("boom")
            return ap.compare_pair(pid, a, b)
        st = ap.run_shard(recs, 0, 1, d, compare=bad)
        self.assertGreater(st["errors"], 0)
        res, fails, _, _ = ap.collect_results([d])
        self.assertGreater(len(fails), 0)
        self.assertFalse(ap.verify_completeness(recs, res)["exact_match"])
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        self.assertEqual(ap.aggregate(recs, [d], out, strict=True), 2)

    def test_unprocessed_budget_not_pass(self):
        recs = corpus()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        st = ap.run_shard(recs, 0, 1, d, max_seconds=0)
        self.assertTrue(st["stopped_by_budget"])
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        self.assertEqual(ap.aggregate(recs, [d], out), 2)

    def test_full_aggregate_pass_and_categories(self):
        recs = corpus()
        d, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.addCleanup(shutil.rmtree, out)
        self.run_all(recs, 3, d)
        self.assertEqual(ap.aggregate(recs, [d], out), 0)
        res = ap.collect_results([d])[0]
        cats = {r["category"] for r in res}
        self.assertIn("diff_lang_same_theme", cats)
        self.assertIn("same_lang_diff_theme", cats)
        self.assertIn("same_lang_same_theme", cats)

    def test_resume_rejects_changed_text(self):
        recs = corpus()
        d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d1)
        self.addCleanup(shutil.rmtree, d2)
        ap.run_shard(recs, 0, 1, d1)
        changed = corpus()
        changed[0]["text"] += "追記"
        changed[0]["text_sha256"] = ap.sha256(changed[0]["text"])
        with self.assertRaises(ap.ManifestMismatch):
            ap.run_shard(changed, 0, 1, d2, resume_dirs=[d1])
        with self.assertRaises(ap.ManifestMismatch):  # different shard layout
            ap.run_shard(recs, 0, 2, d2, resume_dirs=[d1])

    def test_resume_rejects_missing_manifest_and_metric_change(self):
        recs = corpus()
        d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d1)
        self.addCleanup(shutil.rmtree, d2)
        ap.run_shard(recs, 0, 1, d1)
        old = ap.METRIC_VERSION
        ap.METRIC_VERSION = "other"
        try:
            with self.assertRaises(ap.ManifestMismatch):
                ap.run_shard(recs, 0, 1, d2, resume_dirs=[d1])
        finally:
            ap.METRIC_VERSION = old
        os.remove(os.path.join(d1, "shard-000.manifest.json"))
        with self.assertRaises(ap.ManifestMismatch):
            ap.run_shard(recs, 0, 1, d2, resume_dirs=[d1])

    def test_aggregate_rejects_stale_results_and_reports_duplicates(self):
        recs = corpus()
        d, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.addCleanup(shutil.rmtree, out)
        self.run_all(recs, 1, d)
        # identical duplicate in a second dir is reported, not hidden
        d2 = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d2)
        for fn in os.listdir(d):
            shutil.copy(os.path.join(d, fn), d2)
        self.assertEqual(ap.aggregate(recs, [d, d2], out, shards=1), 0)
        import json
        comp = json.load(open(os.path.join(out, "completeness.json")))
        self.assertEqual(comp["raw_duplicate_lines_identical"], 15)
        self.assertEqual(comp["duplicates"], 0)
        # results computed for different text -> aggregate against new snapshot fails
        changed = corpus()
        changed[0]["text"] += "追記"
        changed[0]["text_sha256"] = ap.sha256(changed[0]["text"])
        self.assertEqual(ap.aggregate(changed, [d], out, shards=1), 2)

    def test_aggregate_validates_fields_and_conflicts(self):
        import json
        recs = corpus()
        d, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.addCleanup(shutil.rmtree, out)
        self.run_all(recs, 1, d)
        p = os.path.join(d, "shard-000.jsonl")
        lines = [json.loads(l) for l in open(p, encoding="utf-8")]
        for field, val in (("category", "diff_lang_diff_theme"), ("len_a", 1), ("url_b", "https://x/zzz")):
            bad = [dict(l) for l in lines]
            bad[0][field] = val
            with open(p, "w", encoding="utf-8") as f:
                for l in bad:
                    f.write(json.dumps(l, ensure_ascii=False) + "\n")
            self.assertEqual(ap.aggregate(recs, [d], out, shards=1), 2, field)
        conflict = [dict(l) for l in lines] + [dict(lines[0], sym=0.123)]
        with open(p, "w", encoding="utf-8") as f:
            for l in conflict:
                f.write(json.dumps(l, ensure_ascii=False) + "\n")
        self.assertEqual(ap.aggregate(recs, [d], out, shards=1), 2)

    def test_unique_stats_tie_break_order_invariant(self):
        t = rnd_text(7, 800)
        recs = [rec("https://x/a", t), rec("https://x/b", t), rec("https://x/c", t)]
        outs = []
        for order in (recs, list(reversed(recs))):
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d)
            res = self.run_all(order, 1, d)
            for ordering in (res, list(reversed(res))):
                outs.append(ap.page_unique_stats(recs, ordering))
        for o in outs[1:]:
            self.assertEqual(o, outs[0])
        self.assertEqual([x["peer"] for x in outs[0]], ["https://x/b", "https://x/a", "https://x/a"])

    def test_snapshot_hash_verified(self):
        import json
        recs = corpus()
        snap = {"articles": recs}
        recs[0]["text"] += "改ざん"
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "s.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        with self.assertRaises(ValueError):
            ap.load_snapshot(p)

    def test_build_snapshot_excludes_homepage(self):
        cp = {"pages": [
            {"url": "https://app-navi.biz/", "theme": "homepage", "language": "ja", "_text": "home"},
            {"url": "https://app-navi.biz/b-2025", "theme": "x", "language": "ja", "slug": "b-2025", "_text": "bb"},
            {"url": "https://app-navi.biz/a", "theme": "x", "language": "en", "slug": "a", "_text": "aa"}]}
        s = bs.build(cp, {}, "2026-11-05T11:21:34Z")
        self.assertEqual([a["url"] for a in s["articles"]], ["https://app-navi.biz/a", "https://app-navi.biz/b-2025"])
        self.assertEqual(s["articles"][1]["year"], 2025)


if __name__ == "__main__":
    unittest.main()
