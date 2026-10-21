import unittest

from population_fix import build
from population_fix.rules import classify_origin


def fake_extract(year, direction, kind, path, dests):
    out = {}
    for code, total in dests.items():
        vals = {9: total, 10: total - 1, 11: 1} if kind == "nationality" else \
            {9: total, 10: 1, 11: 1, 12: 1, 13: 1, 14: 1, 15: 1, 16: 1, 17: total - 7,
             18: total - 1, 27: 1}
        out[code] = {"sheet": code, "sheet_name_text": f"県{code}", "total_row": 15, "vals": vals,
                     "origin_labels": {}, "excluded_rows": {}, "excluded_max": {},
                     "non_numeric_cells": 0, "integrity": [], "hidden_origin": {},
                     "ranked_candidates": {},
                     "ranks": {k: [("01101", 5)] for k in (
                         "total", "nationality:japanese", "nationality:foreign", "sex:male",
                         "sex:female", "age:0-19", "age:20-39", "age:40-59", "age:60+")}}
    return {"year": year, "kind": kind, "direction": direction, "path": path, "dests": out,
            "failures": []}


class MergeAllPrefectures(unittest.TestCase):
    def test_47_prefectures_are_all_kept(self):
        extracts = []
        expected = set()
        for pref in range(1, 48):
            code = f"{pref:02d}201"
            expected.add(code)
            extracts.append(fake_extract(2025, "inflow", "nationality", f"p{pref:02d}.xlsx",
                                         {code: 100 + pref}))
        res = build.assemble(extracts)
        self.assertEqual(set(res["pop"]["2025"]["inflow"]), expected)
        self.assertEqual(len(res["attr"]["nationality"]["2025"]["inflow"]["japanese"]), 47)
        self.assertEqual(res["issues"]["duplicate_destination"], [])

    def test_duplicate_destination_is_reported_not_overwritten(self):
        a = fake_extract(2025, "inflow", "nationality", "a.xlsx", {"13104": 111})
        b = fake_extract(2025, "inflow", "nationality", "b.xlsx", {"13104": 222})
        res = build.assemble([b, a])
        self.assertEqual(res["pop"]["2025"]["inflow"]["13104"]["total"], 111)   # first by path
        self.assertEqual(len(res["issues"]["duplicate_destination"]), 1)

    def test_directions_and_years_stay_separate(self):
        ex = [fake_extract(y, d, "nationality", f"{y}{d}.xlsx", {"13104": 10 * y % 997})
              for y in (2019, 2021, 2025) for d in ("inflow", "outflow")]
        res = build.assemble(ex)
        for y in ("2019", "2021", "2025"):
            self.assertEqual(set(res["pop"][y]), {"inflow", "outflow"})


class Classification(unittest.TestCase):
    def test_row_kinds(self):
        self.assertEqual(classify_origin("00000", "総数"), "total")
        self.assertEqual(classify_origin("04000", "04宮城県"), "prefecture_total")
        self.assertEqual(classify_origin("04100", "100仙台市"), "city_total")
        self.assertEqual(classify_origin("13100", "100特別区部"), "special_wards_total")
        self.assertEqual(classify_origin("04199", "その他の区"), "other_wards")
        self.assertEqual(classify_origin("01999", "その他の市町村"), "other_municipalities")
        self.assertEqual(classify_origin("04101", "101青葉区"), "individual")
        self.assertEqual(classify_origin("11108", "108南区"), "individual")
        self.assertEqual(classify_origin("13114", "114中野区"), "individual")


if __name__ == "__main__":
    unittest.main()
