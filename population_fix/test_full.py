import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import openpyxl

from population_audit.common import write_json
from population_fix import build, full, national, render

FIX = Path(__file__).parent / "fixtures" / "renderRanking_current.js"


def make_workbook(path, kind="nationality"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "13104東京都新宿区"
    ws.append([None] * 10 + ["第３表 移動前の住所地別転入者数"])
    width = 12 if kind == "nationality" else 36

    def row(code, label, vals):
        r = [None] * width
        r[7], r[8] = label, code
        for i, v in enumerate(vals):
            r[9 + i] = v
        return r
    if kind == "nationality":
        ws.append(row("00000", "総数", [100, 70, 30]))
        ws.append(row("13000", "13東京都", [60, 40, 20]))
        ws.append(row("13114", "114中野区", [30, 20, 10]))
        ws.append(row("13116", "116豊島区", [20, 15, 5]))
        ws.append(row("14100", "100横浜市", [25, 20, 5]))
        ws.append(row("14199", "その他の区", [9, 8, 1]))
    wb.save(path)


class Roundtrip(unittest.TestCase):
    def test_json_roundtrip_keeps_totals_and_rankings(self):
        tmp = Path(tempfile.mkdtemp())
        make_workbook(tmp / "n.xlsx")
        ex = build.extract_file(tmp / "n.xlsx", 2025, "nationality", "inflow")
        direct = build.assemble([dict(ex, path="n.xlsx")])
        full._gz_write(tmp / "e.json.gz", ex)
        back = build.normalize_extract(full._gz_read(tmp / "e.json.gz"))
        viajson = build.assemble([dict(back, path="n.xlsx")])
        self.assertEqual(direct["pop"], viajson["pop"])
        rec = viajson["pop"]["2025"]["inflow"]["13104"]
        self.assertEqual(rec["total"], 100)                                   # 総数行を一度だけ
        self.assertEqual([t["code"] for t in rec["top10"]], ["13114", "13116"])  # 集約行は順位に入らない

    def test_without_normalization_the_bug_would_empty_everything(self):
        tmp = Path(tempfile.mkdtemp())
        make_workbook(tmp / "n.xlsx")
        ex = build.extract_file(tmp / "n.xlsx", 2025, "nationality", "inflow")
        raw = json.loads(json.dumps(ex))             # integer keys became strings
        res = build.assemble([dict(raw, path="n.xlsx")])
        self.assertEqual(res["pop"], {})              # shows why normalize_extract is required


class Listing(unittest.TestCase):
    def lst(self, n=94, shuffle=False, dup=False):
        items = []
        for i in range(n):
            exp = i % 47
            items.append({"pref_code": f"{exp + 1:02d}", "direction_assumed": "inflow" if i < 47 else "outflow",
                          "order_ok": True})
        if dup:
            items[1] = dict(items[0])
        if shuffle:
            items[3]["order_ok"] = False
        return SimpleNamespace(items=items, warnings=[])

    def test_good(self):
        self.assertEqual(full.listing_problems(self.lst()), [])

    def test_bad_cases(self):
        self.assertTrue(full.listing_problems(self.lst(n=93)))
        self.assertTrue(full.listing_problems(self.lst(shuffle=True)))
        self.assertTrue(full.listing_problems(self.lst(dup=True)))
        bad = self.lst()
        bad.warnings = ["x"]
        self.assertTrue(full.listing_problems(bad))


class Gates(unittest.TestCase):
    def candidate(self, tmp):
        for k in ("total", "nationality", "sex", "age", "age_unknown"):
            full._gz_write(tmp / "json" / f"{k}.json.gz", {})
        return tmp

    def test_verify_year_flags_missing_official_file(self):
        tmp = Path(tempfile.mkdtemp())
        self.candidate(tmp)
        man = {"files": [{"stem": "2025_nationality_1", "kind": "nationality", "direction": "inflow",
                          "status": "completed", "official_file": "nope.xlsx"}], "scope_filtered": False}
        write_json(tmp / "m.json", man)
        (tmp / "work" / "official").mkdir(parents=True)
        args = argparse.Namespace(year="2025", candidate=str(tmp), manifest=str(tmp / "m.json"),
                                  work=str(tmp / "work"), out=str(tmp), expected_files=188)
        full.cmd_verify_year(args)
        res = json.loads((tmp / "verification_2025.json").read_text())
        self.assertFalse(res["passed"])
        self.assertEqual(len(res["files_not_verified"]), 1)
        self.assertFalse(res["scope_complete"])

    def test_finalize_never_marks_verified_on_partial_scope(self):
        tmp = Path(tempfile.mkdtemp())
        write_json(tmp / "cand" / "build_summary.json", {"complete": False, "blockers": ["2019: extraction manifest missing"]})
        write_json(tmp / "ver" / "verification_2025.json", {"year": 2025, "passed": True, "checks": 1,
                   "rankings_values_checked": 1, "failure_count": 0, "files_verified": 188, "expected_files": 188,
                   "verification_checks_passed": True, "scope_complete": True, "files_not_verified": []})
        args = argparse.Namespace(candidate=str(tmp / "cand"), verifications=str(tmp / "ver"),
                                  out=str(tmp / "out"), expected_years="2019,2021,2025")
        full.cmd_finalize(args)
        res = json.loads((tmp / "out" / "verification_final.json").read_text())
        self.assertFalse(res["overall_verified"])
        self.assertFalse(res["fully_complete"])
        self.assertIn("2019: no verification result", res["blockers"])


class National(unittest.TestCase):
    def test_old_city_data_numbers_do_not_survive(self):
        html = ("x\nconst CITY_DATA={\"2025\": {\"inflow\": {\"13104\": {\"name\": \"旧\", \"top10\": "
                "[{\"code\": \"1\", \"name\": \"a\", \"value\": 987654}]}}}};\n"
                "const SEARCH_INDEX=[{\"code\": \"99999\", \"name\": \"旧\", \"region\": \"r\", \"slug\": \"kanto\"}];\ny")
        pop = {"2025": {"inflow": {"13104": {"name": "東京都新宿区", "total": 5, "top10": []}}, "outflow": {}}}
        idx = national.build_search_index(pop, {"kanto": "関東"})
        out = national.patch_national(html, national.national_city_data(pop), idx)
        self.assertNotIn("987654", out)
        self.assertNotIn("99999", out)
        self.assertEqual(render.read_const_json(out, "CITY_DATA")["2025"]["inflow"]["13104"]["name"], "東京都新宿区")

    def test_pref_all_mismatch_and_missing_are_not_passed(self):
        pa = {"2025": {"13000": {"name": "東京都", "net_migration": 100}, "14000": {"name": "神奈川県", "net_migration": 5}}}
        r = national.check_pref_all(pa, {"2025": {"13000": 100, "14000": 6}})
        self.assertFalse(r["passed"])
        self.assertEqual(r["mismatch_count"], 1)
        r2 = national.check_pref_all(pa, {"2025": {"13000": 100}})
        self.assertFalse(r2["passed"])
        self.assertEqual(r2["not_checkable_count"], 1)


class DisplayPatch(unittest.TestCase):
    def test_patch_on_the_real_renderRanking_code(self):
        src = "<script>\n" + FIX.read_text(encoding="utf-8") + "function renderPanel(){}\n</script>"
        out = render.patch_display(src)
        body = out.split("function renderPanel")[0]
        self.assertNotIn("currentYear==='2019'", body)
        self.assertIn("ranking_unavailable", body)
        self.assertIn("ranking_note", body)
        self.assertIn("typeof info.total!=='number'", body)

    def test_patch_fails_loudly_if_code_changed(self):
        with self.assertRaises(render.PatchError):
            render.patch_display("<script>function renderRanking(){}</script>")


if __name__ == "__main__":
    unittest.main()
