# Overnight official-value comparisons

This workflow performs actual numeric comparisons, independently parsing official
sources instead of using generated website data as the reference. It does not
publish, modify WordPress, upload to the server, merge, or fix data automatically.

Implemented scopes:

* Station: published 2023/2024 map data, 2019/2021/2023/2024 passenger fields,
  compared to MLIT S12-25 original GeoJSON. Non-observed availability codes and
  ambiguous name/operator/line keys stay unverified. The edition may revise
  historical values; reported differences require source-edition review.
* Station ranking: all passenger values in both shared ranking source datasets,
  using the same official table. This does not certify ordering or geography.
* Airport: all published 2019/2021/2023/2024 domestic, international and total
  passenger fields, compared to the MLIT calendar-year workbook. Fiscal years,
  cargo and helicopter facilities are excluded from reference matching.
* Road: every published section's 2015/2021 24h traffic and length across nine
  Japanese regional point files, compared by basic section ID to official
  prefecture CSVs. 2015 is the comparison value included in the 2021 source.
* Onsen: 47 prefectures x five fields x five language copies, compared to the
  environment ministry FY2024 PDF. Does not cover GSJ chemical sampling points.
* Schools: original nine e-Stat tables for 1965/1990/2024, independently parsed
  with prefecture labels and year guards, compared to six school-count fields
  per published prefecture row. Student counts and national summaries remain
  outside this adapter. Okinawa1965 and the documented Kanagawa correction stay
  explicitly unverified instead of being counted as ordinary matches.

**Port, medical, minerals and hotel/ryokan do not yet have numerical adapters.**
They produce explicit incomplete reports with zero comparisons; they cannot pass.
Existing aging and infection adapters remain in `data_audit.run` and are run by
the separate integrity workflow. Its remaining infection gaps still apply.

Every source response is archived with URL, time and SHA256. All mismatches and
unverified cells are retained, not just a sample. Failed sources and themes do
not stop other themes. Missing artifacts are marked missing in the morning
summary. A green Actions run means report generation succeeded, not that every
theme has been verified. `full_audit_complete` remains false.
