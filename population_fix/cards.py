"""Regional data cards (/cards/population/<lang>/<slug>_datacard.html).

Each card shows three values (2019/2021/2025): the sum of prefecture net migration
(転入 - 転出, between prefectures) over the prefectures of the region.  They are checked
against the official prefecture sheets (pref_net_from_extracts).  Only mismatching cards get
a work-copy candidate; originals are never modified.
"""
from __future__ import annotations

import re
from pathlib import Path

from .rules import SLUG_MAP, SLUGS

ROW_RE = re.compile(
    r'(<td class="region-label">[^<]*</td>\s*)'
    r'<td class="(?:pos|neg)">([+\-−]?[\d,]+)</td>\s*'
    r'<td class="(?:pos|neg)">([+\-−]?[\d,]+)</td>\s*'
    r'<td class="(?:pos|neg)">([+\-−]?[\d,]+)</td>', re.S)
YEARS = ("2019", "2021", "2025")


def _int(s: str) -> int:
    return int(s.replace("−", "-").replace(",", "").replace("+", ""))


def parse_card(html: str):
    m = ROW_RE.search(html)
    if not m:
        return None
    return {y: _int(m.group(i + 2)) for i, y in enumerate(YEARS)}


def fmt(v: int) -> str:
    return f"{v:+,}"


def region_sums(pref_net: dict) -> dict:
    """{slug: {year: sum of prefecture net migration}}; None if any prefecture is unavailable."""
    out = {s: {} for s in SLUGS}
    for y in YEARS:
        per = pref_net.get(y, {})
        for slug in SLUGS:
            codes = [f"{p}000" for p, sl in SLUG_MAP.items() if sl == slug]
            vals = [per.get(c) for c in codes]
            out[slug][y] = None if any(v is None for v in vals) else sum(vals)
    return out


def patch_card(html: str, values: dict) -> str:
    def repl(m):
        cells = "".join(
            f'<td class="{"pos" if values[y] >= 0 else "neg"}">{fmt(values[y])}</td>\n    '
            for y in YEARS).rstrip()
        return m.group(1) + cells
    new, n = ROW_RE.subn(repl, html, count=1)
    if n != 1:
        raise ValueError("card row not found")
    return new


def check_cards(cards_dir: Path, pref_net: dict, out_site: Path | None = None) -> dict:
    sums = region_sums(pref_net)
    rows, missing, candidates = [], [], []
    for slug in SLUGS:
        p = Path(cards_dir) / "ja" / f"{slug}_datacard.html"
        if not p.exists():
            missing.append(f"ja/{slug}")
            continue
        html = p.read_text(encoding="utf-8")
        card = parse_card(html)
        if card is None:
            missing.append(f"ja/{slug} (unparsable)")
            continue
        bad = False
        for y in YEARS:
            off = sums[slug][y]
            ok = off is not None and off == card[y]
            bad |= not ok
            rows.append({"lang": "ja", "slug": slug, "year": y, "card": card[y], "official": off, "match": ok,
                         "diff": None if off is None else card[y] - off})
        if bad and out_site is not None and all(sums[slug][y] is not None for y in YEARS):
            dst = Path(out_site) / "cards" / "population" / "ja" / f"{slug}_datacard.html"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(patch_card(html, sums[slug]), encoding="utf-8")
            candidates.append(f"ja/{slug}")
    mism = [r for r in rows if not r["match"]]
    totals = {y: sum(v[y] for v in sums.values() if v[y] is not None) for y in YEARS}
    return {"checked_values": len(rows), "mismatch_count": len(mism), "mismatches": mism,
            "cards_not_checked": missing, "candidates_written": candidates,
            "official_region_sum_total_by_year": totals,
            "other_languages": "cards for en/de/es/fr were not found at /cards/population/<lang>/<slug>_datacard.html (HTTP 404); location unknown -> NOT verified",
            "passed": bool(rows) and not mism and not missing}
