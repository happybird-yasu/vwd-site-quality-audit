# Published population release — 2026-11-03

This directory records the population articles, maps and cards published on
[app-navi.biz](https://app-navi.biz/population-national/) on 2026-11-03.
It is a versioned record of the deployed files, not a deployment command.

- `site/`: the 164 deployed map, card, dictionary, statistical and Hamamatsu-boundary
  files, plus 14 existing shared dependencies. Paths are relative to the public site root.
- `articles/`: ten approved domestic Japanese articles, ten overseas-audience Japanese
  source articles, and forty translations in English, Spanish, German and French.
  The overseas-audience Japanese set is the translation source; it is not a second set
  of Japanese WordPress posts.
- `wordpress/`: the exact saved body of each of the 50 published posts, with the
  five-language article navigation, map shortcode and regional data-card iframe.
- `publication.json`: existing post IDs, titles, URLs, source/body paths, embed URLs and
  shared-dependency provenance. Existing URLs and publication states were preserved.
- `glossary.csv`: the Japanese / English / Spanish / German / French terminology list.
- `SHA256SUMS.json`: checksums for all release payloads and verification records.
- `verification/`: the recorded post-publication checks, containing no WordPress full-site
  export, login information, cookies or local account paths.

## Recorded verification

All 50 saved WordPress bodies and titles matched the approved publication inputs.
The normal public URLs matched the titles, 2,322 paragraph/list/heading elements and
the map/card references. All 164 deployed static files matched their candidate SHA-256
checksums. The other articles and the target posts' classifications were preserved.

The synchronization check caught incorrect `_datacard_ja.html` embed paths in the nine
Japanese regional posts. These were corrected in WordPress to the existing
`_datacard.html` paths before this snapshot was committed. All 45 card URLs were then
fetched successfully and matched the saved assets byte for byte. The offline check
also requires every map/card embed to resolve to a file in this snapshot, so a saved
body matching its input alone is insufficient.

Representative browser checks covered the English Kanto article's map and card, and
the Spanish Chubu/Hokuriku map's 2025 Hamamatsu Chuo data, ranking, romanization and age
view. These records do not assert a native-language proofread of all forty translations,
an all-device check, or fresh official-workbook verification on every later CI run.

Run the offline release check from the repository root:

```sh
python published/population/verify_snapshot.py
```

The existing official-workbook regeneration code remains in `population_fix/` and
`population_audit/`. Its original numerical verification is recorded in
[run 39594922150](https://github.com/happybird-yasu/vwd-site-quality-audit/actions/runs/39594922150).
The release-integrity workflow is separate and does not download the official workbooks
again, modify WordPress, or deploy files.

## Using this record

Map files need the shared geometry and romanization scripts listed in `publication.json`;
map tiles and some libraries are externally hosted. Source attribution and the
Hamamatsu boundary provenance remain in the assets and the generator fixtures.

Keep publication changes within this directory and review the checksums and exact body
diffs. Do not upload this entire repository to the public web root. WordPress full-site
exports and operational backups are deliberately kept outside Git.
