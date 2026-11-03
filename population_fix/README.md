# population_fix - regenerate the population-flow map data from official tables

Candidate files only. Nothing here touches the live site, WordPress or the server, and nothing is
published. Every request to the public site is a read-only GET.

## What it does
1. `extract` (per year): downloads the official e-Stat reference tables (第1/2表 age x sex, 第3/4表
   nationality; inflow/outflow; 47 prefectures) and extracts each destination's **総数 (00000) row once**.
   Subtotal and breakdown rows are never summed. Suppressed / non-numeric cells are never turned into 0:
   the record is omitted and listed in `issues.json`.
2. `assemble`: builds `POP_DATA` and the attribute JSON (sex / age / nationality) with the **existing
   schema**, replaces them in **copies** of the current pages (9 regions x 5 languages + 5 national
   pages), rebuilds `SEARCH_INDEX`, applies the display fixes, writes the diff report and
   `deploy-candidate.zip` (paths relative to the site root).
3. `verify-year`: a separate verifier re-reads the official workbooks directly (it does not use the
   generator's parser) and checks every generated total, split, age-unknown, and top-10 ranking.
4. `cards`: checks the regional data cards against official prefecture totals.
5. `finalize`: the gate. `overall_verified` is true only if all years / 188 files / identity / anchors /
   language equality / page generation / verification passed. `fully_complete` stays false while cards
   (other languages), static article text and in-browser checks are open.

## Ranking rule (tier = `polygon_unit`)
A ranking row is **one individually listed municipality or ward that has its own map polygon**
(市・町・村・特別区・政令市の区). Rows are classified from their real code and label
(`rules.classify_origin`), not from assumptions about which prefecture lists wards:
- never ranked: 総数, 都道府県計 (xx000), 政令市の市全体, 特別区部計, 「その他の区」「その他の市町村」;
- a designated-city total therefore never sits next to its own wards, so nothing is counted twice;
- ties are broken by code; values <= 0 and non-numeric cells are not ranked.
The ranking is "the top of the units listed individually in the official table". Places grouped in
"その他" rows are not ranked, so it is **not a complete ranking of all municipalities**; the total is the
official 総数, not the sum of the ranking. `diff_report` counts destinations where an excluded aggregate
row is larger than the 10th ranked value.

## Other rules
- Age: public buckets 0-19 / 20-39 / 40-59 / 60+ stay as before; 不詳/その他 is written to the new, additive
  `data/<region>/age_unknown.json` (not read by the page). Identity checked: buckets + unknown = total.
- Municipalities that changed between years keep the code published in that year's table; the boundary
  report lists data codes without a current polygon (e.g. 2019/2021 codes that no longer exist).
- Direction comes from each file's own title and is checked against the e-Stat list order.

## Display fixes (page copies only)
- 2019 foreign-national data is no longer blocked unconditionally.
- If a record has an official total but no ranking, the total is shown with an explanation.
- Neutral wording when a value cannot be shown; short reading notes (5 languages) about the ranking and
  about age "unknown".

## Known limits / open items
- Cards in en/de/es/fr: not found at `/cards/population/<lang>/<slug>_datacard.html` (404) -> not verified.
- Static article text, in-browser rendering and map polygons for added/removed codes need a human check.
- GitHub-side stops, cache eviction and e-Stat availability cannot be guaranteed; the run records failures,
  defers work after 325 minutes, and never reports verified in that case.

## Run locally (Tokyo 2025 smoke test)
`python -m population_fix tokyo2025 --sources <dir with 4 Tokyo workbooks> --snapshot <public snapshot> --html <kanto.html> --out <dir>`
