#!/usr/bin/env python3
"""First-pass A/B/C/D review-priority scoring on top of an existing audit report.json.

This is explicitly NOT a publish/unpublish decision. GitHub's role in the
workflow is "produce objective numbers and surface candidates to check" —
page-level usefulness and region-specificity are ChatGPT's/a human's call,
large-scale template-risk and site-structure judgment is Claude's, and the
final decision is the human's. See README.md for the full role split.

Scores each crawled page over the three quantitative axes run_audit.py
already measures (theme-similarity, estimated unique-text ratio, similarity
to the same-language national/all-Japan page), normalized to 100. Each
page also reports how many of those 3 axes it actually had data for
("axes_scored"), since a page scored on 2 axes is less certain than one
scored on all 3.

Deliberately NOT scored here: "region specificity" (chi-iki koyuu-sei) —
report.json no longer carries page text (it's dropped after
compute_similarity() uses it, see parse_page()/write_report()), so judging
whether a page's text contains real region-specific observations needs a
separate text-reading pass by a human or an LLM that reads the page.

Multilingual pages are NOT penalized for being translations of the same
template: run_audit.py's compute_similarity() only ever compares a page
against same-language siblings (an English Kanto page is never compared
against the Japanese original), so a high similarity score here always
means "similar to other same-language pages in this theme," never "this
is a translation."
"""
import argparse
import csv
import glob
import json
import os
from collections import Counter, defaultdict

# Weights for the axes this script can actually compute from existing crawl
# data. "Region specificity" (25 points in the full human judging table) is
# intentionally absent — see module docstring.
WEIGHTS = {"similarity": 30, "uniqueness": 25, "national": 20}
AXES = ("similarity", "uniqueness", "national")

PRIORITY_THEMES = ("infection", "population", "road")
INFECTION_CAVEAT = (
    "infection(感染症週報)は時系列データ更新型のページです。週ごとにデータだけが変わる構造上、"
    "本文の類似率が高くなるのは当然で、それ自体を危険とは判定しないでください。グレード/スコアは"
    "あくまで数値シグナルであり、データの時点・更新頻度を人間/ChatGPTが確認したうえで判断してください。"
)


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
    """Lower similarity to the same-language national/all-Japan page is safer.
    ratio is similarity_to_national (0-1); None covers not_applicable, not_available,
    and skipped_time_budget_exceeded alike — all three mean "no axis to score"."""
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
    """Returns (score_0_100_or_None, {axis: level_or_None}) for one report.json page dict."""
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
    """A/B/C/D here is a *review-priority* signal, not a publish/unpublish decision.
    C or D means "check this page sooner," never "unpublish this page."""
    if score is None:
        return "unscored"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def axes_scored_count(levels):
    return sum(1 for lv in levels.values() if lv is not None)


def priority_level(page):
    """High/Medium/Low review-priority for C/D pages, from the raw percentages
    directly (not from the banded levels), per the internal judging table.
    Returns None for A/B pages — priority only applies to pages already
    flagged for review."""
    score, levels = score_page(page)
    grade = grade_for_score(score)
    if grade not in ("C", "D"):
        return None

    sim = page.get("max_similarity_in_theme")
    uniq = page.get("estimated_unique_ratio")
    sim_pct = sim * 100 if sim is not None else None
    uniq_pct = uniq * 100 if uniq is not None else None

    if (sim_pct is not None and sim_pct >= 90) or (uniq_pct is not None and uniq_pct < 15):
        return "High"
    if (sim_pct is not None and 80 <= sim_pct < 90) or (uniq_pct is not None and 15 <= uniq_pct < 25):
        return "Medium"
    return "Low"


def classify_pages(pages):
    """Returns a list of classification dicts, one per page (homepage excluded:
    it has no theme-peer group, so theme-similarity scoring doesn't apply to it)."""
    results = []
    for p in pages:
        if p.get("theme") == "homepage":
            continue
        score, levels = score_page(p)
        grade = grade_for_score(score)
        results.append({
            "id": p["id"],
            "url": p["url"],
            "theme": p.get("theme"),
            "region": p.get("region"),
            "language": p.get("language"),
            "max_similarity_in_theme": p.get("max_similarity_in_theme"),
            "estimated_unique_ratio": p.get("estimated_unique_ratio"),
            "similarity_to_national": p.get("similarity_to_national"),
            "similarity_to_national_status": p.get("similarity_to_national_status"),
            "levels": levels,
            "axes_scored": axes_scored_count(levels),
            "axes_scored_display": f"{axes_scored_count(levels)}/{len(AXES)}",
            "score": score,
            "grade": grade,
            "priority_level": priority_level(p),
            "region_specificity_pending": True,
            "priority_review": p.get("theme") in PRIORITY_THEMES,
        })
    return results


def find_latest_report_json(results_dir="results"):
    candidates = sorted(glob.glob(os.path.join(results_dir, "run-*", "report.json")))
    return candidates[-1] if candidates else None


def _breakdown(cd_pages, key):
    counts = Counter(c[key] for c in cd_pages)
    return sorted(counts.items(), key=lambda kv: -kv[1])


PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def _cd_sort_key(c):
    return (PRIORITY_ORDER.get(c["priority_level"], 3), c["score"] if c["score"] is not None else -1)


def _table(lines, header, rows, formatter):
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        lines.append("| " + " | ".join(formatter(row)) + " |")
    lines.append("")


def write_outputs(classified, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "magi_classification.json"), "w", encoding="utf-8") as f:
        json.dump({"pages": classified}, f, ensure_ascii=False, indent=2)

    grade_counts = Counter(c["grade"] for c in classified)
    cd_pages = sorted((c for c in classified if c["grade"] in ("C", "D")), key=_cd_sort_key)
    priority_counts = Counter(c["priority_level"] for c in cd_pages)
    priority = sorted((c for c in classified if c["priority_review"]), key=_cd_sort_key)

    cd_row_cols = ["url", "theme", "language", "region", "grade", "score",
                   "axes_scored", "max_similarity_in_theme", "estimated_unique_ratio",
                   "similarity_to_national", "priority_level"]

    def cd_formatter(c):
        return [
            c["url"], c["theme"], c["language"], str(c["region"]), c["grade"], str(c["score"]),
            c["axes_scored_display"], str(c["max_similarity_in_theme"]), str(c["estimated_unique_ratio"]),
            str(c["similarity_to_national"]), str(c["priority_level"]), "true",
        ]

    lines = []
    lines.append("# MAGI First-Pass Review-Priority Classification (A/B/C/D)\n")
    lines.append(
        "**これは最終判定ではなく、数値上の精査優先度です。** C/Dだからといって即・下書き化/公開停止には"
        "しないでください。役割分担: GitHub=客観データと候補抽出のみ、ChatGPT=ページ単体の利用価値・地域"
        "固有性の確認、Claude=大量テンプレ感・サイト全体構造の確認、人間=最終判断。\n"
    )
    lines.append(
        "スコアは3つの数値軸(テーマ内類似率30pt、固有文比率25pt、同一言語の全国版との類似率20pt)を"
        "100点に正規化したものです。**地域固有性(本来25点分)はここでは自動採点していません**——本文を"
        "読まないと判断できないため、全ページに `region_specificity_pending: true` が付きます。\n"
    )
    lines.append(
        "**多言語ページについて:** 日本語原本と英語/独語/スペイン語/フランス語版が同じ内容であること自体は"
        "問題にしていません。類似率の比較は同一言語内のみで行っており(英関東 vs 英近畿、のように)、"
        "翻訳であることを理由に減点はしていません。\n"
    )
    lines.append(f"- Total pages classified: {len(classified)}")
    for g in ("A", "B", "C", "D", "unscored"):
        if grade_counts.get(g):
            lines.append(f"- Grade {g}: {grade_counts[g]}")
    lines.append(f"- C/D total: {len(cd_pages)}")
    for level in ("High", "Medium", "Low"):
        if priority_counts.get(level):
            lines.append(f"- Priority {level} (C/D only): {priority_counts[level]}")
    lines.append("")

    lines.append("## C/D breakdown by theme\n")
    _table(lines, ["Theme", "C/D count"], _breakdown(cd_pages, "theme"), lambda kv: [kv[0], str(kv[1])])

    lines.append("## C/D breakdown by language\n")
    _table(lines, ["Language", "C/D count"], _breakdown(cd_pages, "language"), lambda kv: [kv[0], str(kv[1])])

    lines.append("## C/D breakdown by region\n")
    _table(lines, ["Region", "C/D count"], _breakdown(cd_pages, "region"), lambda kv: [str(kv[0]), str(kv[1])])

    lines.append(f"## Priority review themes ({', '.join(PRIORITY_THEMES)})\n")
    lines.append(f"**注意:** {INFECTION_CAVEAT}\n")
    if priority:
        _table(lines, cd_row_cols + ["region_specificity_pending"], priority, cd_formatter)
    else:
        lines.append("None of the priority themes appear in this run's pages.\n")

    lines.append("## Grade C / D pages (numeric signal only — confirm region specificity and page-level "
                  "value before unpublishing anything)\n")
    if cd_pages:
        _table(lines, cd_row_cols + ["region_specificity_pending"], cd_pages, cd_formatter)
    else:
        lines.append("None.\n")

    with open(os.path.join(out_dir, "magi_classification.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    with open(os.path.join(out_dir, "magi_review_queue.csv"), "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cd_row_cols + ["region_specificity_pending"])
        for c in cd_pages:
            writer.writerow(cd_formatter(c))

    return grade_counts, cd_pages, priority_counts


def print_summary(grade_counts, cd_pages, priority_counts):
    print(f"Grade counts: " + ", ".join(f"{g}={grade_counts[g]}" for g in ("A", "B", "C", "D", "unscored") if grade_counts.get(g)))
    print(f"C/D total: {len(cd_pages)}")
    print("Priority counts (C/D only): " + ", ".join(
        f"{level}={priority_counts[level]}" for level in ("High", "Medium", "Low") if priority_counts.get(level)))

    theme_counts = Counter(c["theme"] for c in cd_pages)
    top10 = sorted(theme_counts.items(), key=lambda kv: -kv[1])[:10]
    print("Top themes by C/D count: " + ", ".join(f"{theme} ({n})" for theme, n in top10))


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
    grade_counts, cd_pages, priority_counts = write_outputs(classified, out_dir)
    print(f"Classified {len(classified)} pages. Written to {out_dir}/magi_classification.md")
    print_summary(grade_counts, cd_pages, priority_counts)


if __name__ == "__main__":
    main()
