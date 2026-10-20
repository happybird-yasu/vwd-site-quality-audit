import unittest

from population_audit import official as off
from population_audit.compare import Tally, compare_sheet, public_internal_checks


def make_rows(total_row, others, title="第３表 移動前の住所地別転入者数"):
    """Synthetic sheet rows with the real column layout (code in col I, total in J)."""
    rows = [(None,) * 12 for _ in range(14)]
    rows[3] = (None,) * 10 + (title,) + (None,)

    def r(code, a, j, f):
        return (None,) * 8 + (code, a, j, f)
    rows.append(r("00000", *total_row))
    for code, a, j, f in others:
        rows.append(r(code, a, j, f))
    return rows


class FakePub:
    def __init__(self, pop=None, attr=None):
        self.pop = pop or {}
        self.attr = attr or {}

    def codes(self, year, direction):
        return sorted(c for (y, d, c) in self.pop if y == year and d == direction)


def run(rows, pub, kind="nationality"):
    sh = off.parse_sheet("13104新宿区", rows, kind)
    t = Tally()
    compare_sheet(sh, "2025", "inflow", pub, t, "TEST")
    return sh, t


class OfficialParsing(unittest.TestCase):
    def test_direction_and_total(self):
        sh = off.parse_sheet("13104新宿区", make_rows((100, 70, 30), [("13000", 60, 40, 20)]), "nationality")
        self.assertEqual(sh.direction, "inflow")
        self.assertEqual(sh.total_row["vals"][9], 100)
        self.assertEqual(sh.sum_all_numeric, 160)

    def test_missing_total_row_is_error(self):
        rows = make_rows((100, 70, 30), [])
        rows = [r for r in rows if r[8] != "00000"]
        with self.assertRaises(off.LayoutError):
            off.parse_sheet("13104新宿区", rows, "nationality")

    def test_suppressed_cell_is_not_zero(self):
        sh = off.parse_sheet("13104新宿区", make_rows(("-", 70, 30), []), "nationality")
        self.assertIsNone(sh.total_row["vals"][9])
        self.assertTrue(sh.non_numeric)


class Comparison(unittest.TestCase):
    def pub(self, total):
        return FakePub(pop={("2025", "inflow", "13104"): {"name": "新宿区", "total": total, "top10": []}})

    def test_match(self):
        _, t = run(make_rows((100, 70, 30), [("13101", 40, 30, 10)]), self.pub(100))
        self.assertEqual(t.counts["total_vs_official"]["match"], 1)

    def test_subtotal_double_count_is_detected(self):
        rows = make_rows((100, 70, 30), [("13000", 60, 40, 20), ("13101", 40, 30, 10)])
        _, t = run(rows, self.pub(200))  # 100 + 60 + 40 = sum of all rows
        self.assertEqual(t.counts["total_vs_official"]["mismatch"], 1)
        row = next(d for d in t.details if d["check"] == "total_vs_official")
        self.assertIn("double count", row["notes"])

    def test_broken_official_identity_is_flagged(self):
        _, t = run(make_rows((100, 70, 20), []), self.pub(100))
        self.assertEqual(t.counts["official_internal_identities"]["mismatch"], 1)

    def test_suppressed_official_total_is_not_comparable(self):
        _, t = run(make_rows(("-", 70, 30), []), self.pub(0))
        self.assertEqual(t.counts["total_vs_official"]["not_comparable"], 1)

    def test_public_missing_is_reported(self):
        _, t = run(make_rows((100, 70, 30), []), FakePub())
        self.assertEqual(t.counts["total_vs_official"]["public_missing"], 1)

    def test_top10_value_aggregate_and_order(self):
        top = [{"code": "13000", "name": "x", "value": 10}, {"code": "13101", "name": "y", "value": 99},
               {"code": "13101", "name": "y", "value": 40}]
        pub = FakePub(pop={("2025", "inflow", "13104"): {"name": "s", "total": 100, "top10": top}})
        _, t = run(make_rows((100, 70, 30), [("13000", 60, 40, 20), ("13101", 40, 30, 10)]), pub)
        self.assertEqual(t.counts["top10_values_total"]["mismatch"], 2)      # 10 vs 60, 99 vs 40
        self.assertEqual(t.counts["top10_contains_aggregate_row"]["mismatch"], 1)
        self.assertEqual(t.counts["top10_duplicate_origin"]["mismatch"], 1)
        self.assertEqual(t.counts["top10_sorted_descending"]["mismatch"], 1)

    def test_aggregate_destination_sheet_is_skipped(self):
        sh = off.parse_sheet("13000東京都計", make_rows((100, 70, 30), []), "nationality")
        t = Tally()
        compare_sheet(sh, "2025", "inflow", FakePub(), t, "TEST")
        self.assertNotIn("total_vs_official", t.counts)


class PublicInternal(unittest.TestCase):
    def test_direction_reuse_and_sum(self):
        rec = {"name": "a", "total": 5, "top10": [{"code": "1", "value": 5}]}
        attr = {}
        for d in ("inflow", "outflow"):
            attr[("sex", "2025", d, "male", "13104")] = dict(rec)
            attr[("sex", "2025", d, "female", "13104")] = dict(rec, total=6)
        pop = {("2025", d, "13104"): {"name": "a", "total": 11, "top10": []} for d in ("inflow", "outflow")}
        t = Tally()
        public_internal_checks(FakePub(pop, attr), ["2025"], t)
        self.assertEqual(t.counts["direction_reuse_sex"]["mismatch"], 2)
        self.assertEqual(t.counts["public_sum_sex_vs_total"]["match"], 2)


class FailureIsolation(unittest.TestCase):
    def test_bad_sheet_does_not_stop_file(self):
        import tempfile
        from pathlib import Path

        import openpyxl

        from population_audit import run as runmod

        tmp = Path(tempfile.mkdtemp())
        wb = openpyxl.Workbook()
        good = wb.active
        good.title = "13104新宿区"
        for r in make_rows((100, 70, 30), []):
            good.append(list(r))
        bad = wb.create_sheet("13105文京区")          # no total row -> LayoutError
        bad.append([None] * 8 + ["13101", 5, 3, 2])
        path = tmp / "t.xlsx"
        wb.save(path)
        item = {"year": 2025, "kind": "nationality", "direction_assumed": "inflow",
                "stat_inf_id": "X", "url": "u", "pref_code": "13"}
        orig = runmod.off.download
        runmod.off.download = lambda it, d: {**it, "path": str(path), "cached": False, "sha256": "s"}
        try:
            res = runmod._process_item(item, FakePub(), tmp)
        finally:
            runmod.off.download = orig
        self.assertEqual(res["sheets"], 1)
        self.assertEqual(len(res["sheet_failures"]), 1)
        self.assertIn("13105", res["sheet_failures"][0]["sheet"])

    def test_safe_reason_redacts_secrets(self):
        from population_audit.common import safe_reason
        s = safe_reason(RuntimeError("failed token=abc123 Authorization: Bearer xyz"))
        self.assertNotIn("abc123", s)
        self.assertNotIn("xyz", s)


if __name__ == "__main__":
    unittest.main()
