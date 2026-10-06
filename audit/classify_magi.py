#!/usr/bin/env python3
"""First-pass MAGI-style A/B/C/D classification on top of an existing audit report.json.

Reads the per-page quantitative signals run_audit.py already computed
(theme-similarity, estimated unique-text ratio, similarity to the national
page) and scores them against the internal VWD-by-Data judging table.

Deliberately NOT scored here: "region specificity" (chi-iki koyuu-sei). That
axis requires reading what a page's body text actually says, and report.json
no longer carries page text (it's dropped after compute_similarity() uses it,
see parse_page()/write_report()). Scoring it well would mean re-fetching and
re-reading all 636 pages' text, which is a separate, heavier pass. So this
script normalizes its score over only the three quantitative axes it has
(30 + 25 + 20 = 75 points) and flags every page as
"region_specificity_pending" rather than fabricating a number for it.
"""
import argparse
import glob
import json
import os
from collections import Counter

# Weights for the axes this script can actually compute from existing crawl
# data. "Region specificity" (25 points in the human judging table) is
# intentionally absent — see module docstring.
WEIGHTS = {"similarity": 30, "uniqueness": 25, "national": 20}

PRIORITY_THEMES = ("infection", "population", "road")


def similarity_level(ratio):
    """Lower theme-similarity is safer. ratio is max_similarity_in_theme (0-1)."""
    if ratio is None:
        return None
    pct = ratio * 100
    if pct < 70:
        return 3
    if pct < 80:
        return 2
    if pct < 90:
        return 1
    return 0


def uniqueness_level(ratio):
    """Higher estimated-unique-text ratio is safer. ratio is estimated_unique_ratio (0-1)."""
    if ratio is None:
        return None
    pct = ratio * 100
    if pct >= 40:
        return 3
    if pct >= 25:
        return 2
    if pct >= 15:
        return 1
    return 0


def national_level(ratio):
    """Lower similarity to the national/all-Japan page is safer. ratio is similarity_to_national (0-1)."""
    if ratio is None:
        return None
    pct = ratio * 100
    if pct < 65:
        return 3
    if pct < 75:
        return 2
    if pct < 85:
        return 1
    return 0


def score_page(page):
    """Returns (score_0_100_or_None, {axis: level}) for one report.json page dict."""
    levels = {
        "similarity": similarity_level(page.get("max_similarity_in_theme")),
        "uniqueness": uniqueness_level(page.get("estimated_unique_ratio")),
        "national": national_level(page.get("similarity_to_national")),
    }
    available = [(name, lv) for name, lv in levels.items() if lv is not None]
    if not available:
        return None, levels

    max_total = sum(WEIGHTS[name] for name, _ in available)
    raw = sum((lv / 3) * WEIGHTS[name] for name, lv in available)
    score = round(raw / max_total * 100, 1)
    return score, levels


def grade_for_score(score):
    if score is None:
        return "unscored"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def classify_pages(pages):
    """Returns a list of classification dicts, one per page (homepage excluded:
    it has no theme-peer group, so theme-similarity scoring doesn't apply to it)."""
    results = []
    for p in pages:
        if p.get("theme") == "homepage":
            continue
        score, levels = score_page(p)
        results.append({
            "id": p["id"],
            "url": p["url"],
            "theme": p.get("theme"),
            "region": p.get("region"),
            "language": p.get("language"),
            "max_similarity_in_theme": p.get("max_similarity_in_theme"),
            "estimated_unique_ratio": p.get("estimated_unique_ratio"),
            "similarity_to_national": p.get("similarity_to_national"),
            "levels": levels,
            "score": score,
            "grade": grade_for_score(score),
            "region_specificity_pending": True,
            "priority_review": p.get("theme") in PRIORITY_THEMES,
        })
    return results


def find_latest_report_json(results_dir="results"):
    candidates = sorted(glob.glob(os.path.join(results_dir, "run-*", "report.json")))
    return candidates[-1] if candidates else None


def write_outputs(classified, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "magi_classification.json"), "w", encoding="utf-8") as f:
        json.dump({"pages": classified}, f, ensure_ascii=False, indent=2)

    grade_counts = Counter(c["grade"] for c in classified)
    priority = [c for c in classified if c["priority_review"]]
    priority.sort(key=lambda c: (c["theme"], -(c["score"] if c["score"] is not None else -1)))
    cd_pages = [c for c in classified if c["grade"] in ("C", "D")]
    cd_pages.sort(key=lambda c: (c["score"] if c["score"] is not None else -1))

    lines = []
    lines.append("# MAGI First-Pass Classification (A/B/C/D)\n")
    lines.append(
        "Provisional classification over the three quantitative axes run_audit.py already "
        "measures: theme-similarity (30pt), estimated unique-text ratio (25pt), similarity to "
        "the national/all-Japan page (20pt), normalized to 100. **Region specificity (25pt in "
        "the full human judging table) is NOT scored here** — it requires reading what each "
        "page's body text actually says, which report.json no longer retains. Every page below "
        "is flagged `region_specificity_pending: true`; a page graded C or D by the numbers alone "
        "may still be fine once a human or a text-reading pass confirms strong region-specific "
        "content (e.g. a weekly-updated infection map with no stable 'content' to be similar).\n"
    )
    lines.append(f"- Total pages classified: {len(classified)}")
    for g in ("A", "B", "C", "D", "unscored"):
        if grade_counts.get(g):
            lines.append(f"- Grade {g}: {grade_counts[g]}")
    lines.append("")

    lines.append(f"## Priority review themes ({', '.join(PRIORITY_THEMES)})\n")
    if priority:
        lines.append("| URL | Theme | Grade | Score | Max sim. in theme | Est. unique ratio | Sim. to national |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in priority:
            lines.append(
                f"| {c['url']} | {c['theme']} | {c['grade']} | {c['score']} | "
                f"{c['max_similarity_in_theme']} | {c['estimated_unique_ratio']} | {c['similarity_to_national']} |"
            )
    else:
        lines.append("None of the priority themes appear in this run's pages.")
    lines.append("")

    lines.append("## Grade C / D pages (numeric signal only — confirm region specificity before unpublishing anything)\n")
    if cd_pages:
        lines.append("| URL | Theme | Grade | Score | Max sim. in theme | Est. unique ratio | Sim. to national |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in cd_pages:
            lines.append(
                f"| {c['url']} | {c['theme']} | {c['grade']} | {c['score']} | "
                f"{c['max_similarity_in_theme']} | {c['estimated_unique_ratio']} | {c['similarity_to_national']} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    with open(os.path.join(out_dir, "magi_classification.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-json", default=None,
                         help="Path to an existing report.json. Defaults to the most recent "
                              "results/run-*/report.json.")
    args = parser.parse_args()

    report_path = args.report_json or find_latest_report_json()
    if not report_path or not os.path.exists(report_path):
        raise SystemExit(f"ERROR: no report.json found (looked for {args.report_json or 'results/run-*/report.json'})")

    with open(report_path, encoding="utf-8") as f:
        data = json.load(f)

    classified = classify_pages(data["pages"])
    out_dir = os.path.dirname(report_path)
    write_outputs(classified, out_dir)
    print(f"Classified {len(classified)} pages. Written to {out_dir}/magi_classification.md")


if __name__ == "__main__":
    main()
