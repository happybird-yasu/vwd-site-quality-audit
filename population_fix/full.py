"""Nationwide regeneration: extract | fetch-pages | assemble | verify-year | finalize.

Failure-tolerant and resumable.  Missing data is never back-filled from the old public
values (that would mix wrong and verified numbers) - affected records are simply absent
and the run is reported incomplete.
"""
from __future__ import annotations

import gzip
import json
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path

from population_audit import official as off
from population_audit.common import (http_get, log_failure, read_json, safe_reason, sha256_bytes,
                                     utcnow, write_json)

from . import build, cards, national, render, verify
from .cli import ATTRS, LANGS, SITE, load_public_names
from .rules import SLUG_MAP, SLUGS, region_of

MAX_FILE_RETRIES = 2
EXTRACT_VERSION = 2          # bump when the extract format changes so cached extracts are rebuilt
BASE = "https://app-navi.biz/maps/population"
SITE_INTERVAL = 1.5


def _gz_write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def _gz_read(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


EXPECTED_FILES_PER_YEAR = 188          # 47 prefectures x 2 directions x 2 tables
EXPECTED_YEARS = ("2019", "2021", "2025")


def listing_problems(lst) -> list:
    """Anything unexpected in an e-Stat listing (count, order, duplicates, gaps, warnings)."""
    probs = list(lst.warnings)
    if len(lst.items) != 94:
        probs.append(f"{len(lst.items)} entries, expected 94")
    combos = [(i["pref_code"], i["direction_assumed"]) for i in lst.items]
    want = {(f"{p:02d}", d) for p in range(1, 48) for d in ("inflow", "outflow")}
    if set(combos) != want:
        probs.append(f"missing {sorted(want - set(combos))[:5]} extra {sorted(set(combos) - want)[:5]}")
    if len(set(combos)) != len(combos):
        probs.append("duplicate (prefecture, direction) entries")
    if any(not i["order_ok"] for i in lst.items):
        probs.append("prefecture order differs from JIS order")
    return probs


# ------------------------------------------------------------------ extract

def cmd_extract(a):
    year = int(a.year)
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    deadline = t0 + a.deadline_minutes * 60 if a.deadline_minutes else None
    manifest = {"year": year, "started_at": utcnow(), "files": [], "discovery_failures": []}
    for kind in ("nationality", "agesex"):
        try:
            lst = off.discover(year, kind)
        except Exception as exc:  # noqa: BLE001
            url = off.LIST_URL.format(tclass=off.TCLASS[kind], year=year)
            log_failure(out, f"{year}/{kind}", "discover", exc, 3, url=url)
            manifest["discovery_failures"].append({"kind": kind, "url": url, "reason": safe_reason(exc)})
            continue
        manifest.setdefault("listing_problems", []).extend(f"{kind}: {x}" for x in listing_problems(lst))
        items = lst.items
        if a.only_pref:
            items = [i for i in items if i["pref_code"] in a.only_pref.split(",")]
        if a.max_prefectures:
            items = [i for i in items if int(i["pref_code"]) <= a.max_prefectures]
        for item in items:
            stem = f"{year}_{kind}_{item['stat_inf_id']}"
            sp = work / "state" / f"{stem}.json"
            ep = work / "extracts" / f"{stem}.json.gz"
            base = {"stem": stem, "kind": kind, "direction": item["direction_assumed"],
                    "pref_code": item["pref_code"], "url": item["url"], "stat_inf_id": item["stat_inf_id"]}
            st = read_json(sp) if sp.exists() else {"status": "pending", "retries": 0}
            if st["status"] == "completed" and ep.exists() and st.get("extract_version") == EXTRACT_VERSION:
                manifest["files"].append({**base, **st, "resumed": True})
                continue
            if st.get("retries", 0) >= MAX_FILE_RETRIES:
                manifest["files"].append({**base, "status": "failed", "retries": st["retries"],
                                          "reason": "retry limit reached; " + st.get("reason", "")})
                continue
            if deadline and time.time() > deadline:
                manifest["files"].append({**base, "status": "deferred_deadline"})
                continue
            write_json(sp, {**st, "status": "in_progress"})
            try:
                try:
                    meta = off.download(item, work / "official")
                    ex = build.extract_file(Path(meta["path"]), year, kind, item["direction_assumed"])
                except Exception:  # noqa: BLE001  one retry with a fresh copy if the cached file was bad
                    for c in (work / "official").glob(f"{stem}.*"):
                        c.unlink()
                    meta = off.download(item, work / "official")
                    ex = build.extract_file(Path(meta["path"]), year, kind, item["direction_assumed"])
                if not ex["dests"]:
                    raise off.LayoutError("no destination sheet could be extracted")
                ex["path"] = Path(meta["path"]).name
                ex["meta"] = {k: v for k, v in meta.items() if k != "path"}
                _gz_write(ep, ex)
                partial = bool(ex["failures"])
                for sf in ex["failures"]:
                    log_failure(out, f"{stem}#{sf['sheet']}", "sheet", RuntimeError(sf["reason"]), 0, url=item["url"])
                st = {"status": "partial" if partial else "completed", "extract_version": EXTRACT_VERSION,
                      "retries": st.get("retries", 0) + (1 if partial else 0),
                      "sheets": len(ex["dests"]), "sheet_failures": len(ex["failures"]),
                      "sha256": meta.get("sha256"), "official_file": Path(meta["path"]).name,
                      "bytes": meta.get("bytes"), "served_filename": meta.get("served_filename")}
            except Exception as exc:  # noqa: BLE001
                retries = st.get("retries", 0) + 1
                log_failure(out, stem, "extract", exc, retries, url=item["url"])
                st = {"status": "failed", "retries": retries, "reason": safe_reason(exc)}
            write_json(sp, st)
            manifest["files"].append({**base, **st})
            print(f"[{year}] {stem} pref={item['pref_code']} -> {st['status']} ({time.time() - t0:.0f}s)", flush=True)
    not_done = [f for f in manifest["files"] if f["status"] != "completed"]
    scope_filtered = bool(a.only_pref or a.max_prefectures)
    manifest["scope_filtered"] = scope_filtered
    manifest["expected_files"] = EXPECTED_FILES_PER_YEAR
    manifest["complete"] = bool(
        not not_done and not manifest["discovery_failures"] and not manifest.get("listing_problems")
        and not scope_filtered and len(manifest["files"]) == EXPECTED_FILES_PER_YEAR
        and all(f["status"] == "completed" for f in manifest["files"]))
    manifest["finished_at"] = utcnow()
    write_json(out / f"manifest_{year}.json", manifest)
    write_json(out / f"rerun_targets_{year}.json", {"year": year, "files": not_done,
                                                    "discovery_failures": manifest["discovery_failures"]})
    dst = out / "extracts"
    dst.mkdir(parents=True, exist_ok=True)
    for p in (work / "extracts").glob(f"{year}_*.json.gz"):
        shutil.copy2(p, dst / p.name)
    print(json.dumps({"year": year, "complete": manifest["complete"],
                      "status": dict(Counter(f["status"] for f in manifest["files"]))}))
    return 0


# -------------------------------------------------------------- fetch pages

def cmd_fetch_pages(a):
    out = Path(a.out)
    man = {"retrieved_at": utcnow(), "files": [], "failures": [], "absent": []}

    def get(url, dest, optional=False):
        try:
            body, _, _ = http_get(url, host_key="site", min_interval=SITE_INTERVAL)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            man["files"].append({"url": url, "path": str(dest.relative_to(out)),
                                 "bytes": len(body), "sha256": sha256_bytes(body)})
        except Exception as exc:  # noqa: BLE001
            if optional and getattr(exc, "code", None) in (404, 410):
                man["absent"].append(url)          # definitive "does not exist": not a failure
                return
            man["failures"].append({"url": url, "reason": safe_reason(exc)})
            log_failure(out, str(dest.name), "fetch-page", exc, 3, url=url)

    for lang in LANGS:
        get(f"{BASE}/{lang}/js/i18n.js", out / lang / "js" / "i18n.js")
        for code in LANGS:      # each folder holds a different set of translation files
            get(f"{BASE}/{lang}/i18n/{code}.json", out / lang / "i18n" / f"{code}.json",
                optional=(code != lang))
        get(f"{BASE}/{lang}/national.html", out / lang / "national.html")
        for slug in SLUGS:
            get(f"{BASE}/{lang}/{slug}.html", out / lang / f"{slug}.html")
    for slug in SLUGS:
        get(f"https://app-navi.biz/maps/shared/{slug}.geojson", out / "geo" / f"{slug}.geojson")
        for lg in LANGS:
            fn = cards.card_file(lg, slug)
            get(f"https://app-navi.biz/cards/population/{lg}/{fn}", out / "cards" / lg / fn)
    write_json(out / "pages_manifest.json", man)
    print(f"fetched={len(man['files'])} failures={len(man['failures'])}")
    return 0


# ----------------------------------------------------------------- assemble

def _load_extracts(dirs):
    ex = []
    for d in dirs:
        for p in sorted(Path(d).rglob("*.json.gz")):
            if p.parent.name == "extracts":
                try:
                    ex.append(build.normalize_extract(_gz_read(p)))
                except Exception as exc:  # noqa: BLE001
                    log_failure(Path(d), p.name, "read-extract", exc, 0)
    return ex


def _old_public(snap: Path):
    pop, attr = {}, {a: {} for a in ATTRS}
    for slug in SLUGS:
        p = snap / f"popdata_{slug}.json.gz"
        if not p.exists():
            continue
        for y, dirs in read_json(p).items():
            for d, cs in dirs.items():
                pop.setdefault(y, {}).setdefault(d, {}).update(cs)
        for a_ in ATTRS:
            q = snap / f"attr_{slug}_{a_}.json.gz"
            if q.exists():
                for y, dirs in read_json(q).items():
                    for d, subs in dirs.items():
                        for s, cs in subs.items():
                            attr[a_].setdefault(y, {}).setdefault(d, {}).setdefault(s, {}).update(cs)
    return pop, attr


def diff_report(gen, extracts, old_pop, old_attr):
    rep = {"generated_at": utcnow(), "baseline": "public data fetched in the audit snapshot (see manifest)",
           "total": {}, "attributes": {}, "ranking": {}, "examples": {}}
    for y in sorted(set(gen["pop"]) | set(old_pop)):
        for d in ("inflow", "outflow"):
            new, old = gen["pop"].get(y, {}).get(d, {}), old_pop.get(y, {}).get(d, {})
            added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
            both = set(new) & set(old)
            changed = [c for c in both if new[c]["total"] != old[c]["total"]]
            top_changed = [c for c in both if [(t["code"], t["value"]) for t in new[c]["top10"]]
                           != [(t["code"], t["value"]) for t in old[c]["top10"]]]
            ratios = sorted(old[c]["total"] / new[c]["total"] for c in changed if new[c]["total"])
            rep["total"][f"{y}/{d}"] = {
                "new_records": len(new), "old_records": len(old), "added": len(added), "removed": len(removed),
                "removed_codes": removed[:50], "unchanged_total": len(both) - len(changed),
                "changed_total": len(changed), "top10_changed": len(top_changed),
                "old_over_new_ratio_median": ratios[len(ratios) // 2] if ratios else None,
                "old_over_new_ratio_max": ratios[-1] if ratios else None,
                "added_with_empty_top10": sum(1 for c in added if not new[c]["top10"])}
    for a_ in ATTRS:
        for y in sorted(gen["attr"].get(a_, {})):
            for d in ("inflow", "outflow"):
                for s, cs in gen["attr"][a_][y].get(d, {}).items():
                    old = old_attr[a_].get(y, {}).get(d, {}).get(s, {})
                    both = set(cs) & set(old)
                    rep["attributes"][f"{a_}:{s}/{y}/{d}"] = {
                        "new": len(cs), "old": len(old), "added": len(set(cs) - set(old)),
                        "removed": len(set(old) - set(cs)),
                        "changed_total": sum(1 for c in both if cs[c]["total"] != old[c]["total"]),
                        "unchanged_total": sum(1 for c in both if cs[c]["total"] == old[c]["total"])}
    affected, affected_groups, nodes = Counter(), Counter(), 0
    for e in build.merge_extracts(extracts)[0].values():
        if e["kind"] != "nationality":
            continue
        for code, d in e["dests"].items():
            top = d["ranks"].get("total", [])
            tenth = top[-1][1] if len(top) == 10 else 0
            nodes += 1
            single = d.get("absent_city_max", 0)
            group = max((d["excluded_max"].get(k, 0) for k in ("other_wards", "other_municipalities")), default=0)
            if single > tenth:
                affected[f"{e['year']}/{e['direction']}"] += 1
            if group > tenth:
                affected_groups[f"{e['year']}/{e['direction']}"] += 1
    rep["ranking"] = {"tier": gen["tier"], "destinations_checked": nodes,
                      "destinations_where_a_city_with_no_ward_listed_exceeds_10th_value": dict(affected),
                      "destinations_where_an_excluded_group_row_exceeds_10th_value": dict(affected_groups)}
    for e in build.merge_extracts(extracts)[0].values():
        if e["kind"] == "nationality" and e["year"] == 2025 and "13104" in e["dests"]:
            rep["examples"][f"13104 cities absent from ranking 2025 {e['direction']}"] = {
                "absent_city_totals": e["dests"]["13104"]["absent_cities"]}
    for key in ("2019", "2021", "2025"):
        for d in ("inflow", "outflow"):
            n, o = gen["pop"].get(key, {}).get(d, {}).get("13104"), old_pop.get(key, {}).get(d, {}).get("13104")
            if n:
                rep["examples"][f"13104 新宿区 {key} {d}"] = {"new_total": n["total"], "old_total": (o or {}).get("total")}
    return rep


def diff_markdown(rep) -> str:
    L = ["# Diff report: regenerated data vs. data published before the correction", "",
         "| year/direction | new records | old records | added | removed | totals unchanged | totals changed | top10 changed |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for k, v in rep["total"].items():
        L.append(f"| {k} | {v['new_records']} | {v['old_records']} | {v['added']} | {v['removed']} | "
                 f"{v['unchanged_total']} | {v['changed_total']} | {v['top10_changed']} |")
    L += ["", "Examples (新宿区 13104):"] + [
        (f"- {k}: new {v['new_total']} / old {v['old_total']}" if "new_total" in v else f"- {k}: {v}")
        for k, v in rep["examples"].items()]
    L += ["", f"Ranking tier: `{rep['ranking']['tier']}` (of {rep['ranking']['destinations_checked']} destination sheets checked).",
          "Destinations where a designated city / the 23 wards appears ONLY as a city-total row (no ward listed "
          "individually) and exceeds the 10th ranked value - that city is entirely absent from the ranking: "
          f"{rep['ranking']['destinations_where_a_city_with_no_ward_listed_exceeds_10th_value']}",
          "Destinations where an excluded *group row* (「その他の区/市町村」) exceeds the 10th value "
          "- many small places pooled, not a single competitor: "
          f"{rep['ranking']['destinations_where_an_excluded_group_row_exceeds_10th_value']}"]
    return "\n".join(L) + "\n"


def cmd_assemble(a):
    out, snap, pages = Path(a.out), Path(a.snapshot), Path(a.pages)
    out.mkdir(parents=True, exist_ok=True)
    expected_years = [y for y in a.expected_years.split(",") if y]
    extracts = _load_extracts(a.extracts)
    gen = build.assemble(extracts, load_public_names(snap))
    merged, _dups = build.merge_extracts(extracts)
    old_pop, old_attr = _old_public(snap)
    site = out / SITE
    issues = {"pages": [], "slug_map_mismatch": [], "national": []}
    pages_written = national_written = 0
    i18n_matrix = []
    # search index: current-boundary municipalities, regenerated from the data
    ref_page = next((pages / lg / "kanto.html" for lg in LANGS if (pages / lg / "kanto.html").exists()), None)
    old_index = render.read_const_json(ref_page.read_text(encoding="utf-8"), "SEARCH_INDEX") if ref_page else []
    polygon_codes = None
    gdir = pages / "geo"
    if gdir.exists() and all((gdir / f"{s}.geojson").exists() for s in SLUGS):
        polygon_codes = set()
        for s in SLUGS:
            polygon_codes |= national.geometry_codes(json.loads((gdir / f"{s}.geojson").read_text(encoding="utf-8")))
    new_index = national.build_search_index(gen["pop"], national.slug_region_names(old_index), polygon_codes)
    city_data = national.national_city_data(gen["pop"])
    pref_all_ref = None
    for lang in LANGS:
        for slug in SLUGS:
            src = pages / lang / f"{slug}.html"
            if not src.exists():
                issues["pages"].append({"lang": lang, "slug": slug, "reason": "page copy missing"})
                continue
            try:
                html = src.read_text(encoding="utf-8")
                if render.read_slug_map(html) != SLUG_MAP:
                    issues["slug_map_mismatch"].append({"lang": lang, "slug": slug})
                html = render.replace_const_json(html, "POP_DATA", render.region_pop(gen["pop"], slug, region_of))
                html = render.replace_const_json(html, "SEARCH_INDEX", new_index)
                html = render.patch_display(html)
                dst = site / lang / f"{slug}.html"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(html, encoding="utf-8")
                pages_written += 1
            except Exception as exc:  # noqa: BLE001
                issues["pages"].append({"lang": lang, "slug": slug, "reason": safe_reason(exc)})
                log_failure(out, f"{lang}/{slug}", "render", exc, 0)
        nsrc = pages / lang / "national.html"
        if not nsrc.exists():
            issues["national"].append({"lang": lang, "reason": "national page copy missing"})
        else:
            try:
                nh = nsrc.read_text(encoding="utf-8")
                pref_all_ref = pref_all_ref or render.read_const_json(nh, "PREF_ALL")
                nh = national.patch_national(nh, city_data, new_index)
                d = site / lang / "national.html"
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_text(nh, encoding="utf-8")
                back = render.read_const_json(d.read_text(encoding="utf-8"), "CITY_DATA")
                if back != city_data:
                    issues["national"].append({"lang": lang, "reason": "CITY_DATA read-back differs"})
                national_written += 1
            except Exception as exc:  # noqa: BLE001
                issues["national"].append({"lang": lang, "reason": safe_reason(exc)})
                log_failure(out, f"{lang}/national", "render", exc, 0)
        # js/i18n.js (Japanese fallback dictionary) of this folder
        sp = pages / lang / "js" / "i18n.js"
        try:
            d = site / lang / "js" / "i18n.js"
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(render.patch_i18n_js(sp.read_text(encoding="utf-8"), lang), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            issues["pages"].append({"lang": lang, "file": "js/i18n.js", "reason": safe_reason(exc)})
        # translation dictionaries: every folder gets all five canonical dictionaries (?lang=xx works anywhere)
        for code in LANGS:
            canon = pages / code / "i18n" / f"{code}.json"
            if not canon.exists():
                issues["pages"].append({"lang": lang, "file": f"i18n/{code}.json",
                                        "reason": f"canonical dictionary {code}/i18n/{code}.json missing"})
                continue
            try:
                txt = json.dumps(render.patch_messages_json(json.loads(canon.read_text(encoding="utf-8")), code),
                                 ensure_ascii=False)
                d = site / lang / "i18n" / f"{code}.json"
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_text(txt, encoding="utf-8")
                prod = pages / lang / "i18n" / f"{code}.json"
                i18n_matrix.append({"folder": lang, "code": code,
                                    "production_file_existed": prod.exists(),
                                    "production_differs_from_canonical": prod.exists() and prod.read_bytes() != canon.read_bytes()})
            except Exception as exc:  # noqa: BLE001
                issues["pages"].append({"lang": lang, "file": f"i18n/{code}.json", "reason": safe_reason(exc)})
    for slug in SLUGS:
        base = site / "data" / slug
        for x in ATTRS:
            render.write_json(base / f"{x}.json", render.region_attr(gen["attr"].get(x, {}), slug, region_of))
        au = {y: {d: {c: r for c, r in cs.items() if region_of(c) == slug} for d, cs in dd.items()}
              for y, dd in gen["age_unknown"].items()}
        render.write_json(base / "age_unknown.json", au)
    for name, obj in (("total", gen["pop"]), ("nationality", gen["attr"].get("nationality", {})),
                      ("sex", gen["attr"].get("sex", {})), ("age", gen["attr"].get("age", {})),
                      ("age_unknown", gen["age_unknown"])):
        _gz_write(out / "json" / f"{name}.json.gz", obj)
    rep = diff_report(gen, extracts, old_pop, old_attr)
    write_json(out / "diff_report.json", rep)
    (out / "diff_report.md").write_text(diff_markdown(rep), encoding="utf-8")
    generated = {"total": gen["pop"], **{x: gen["attr"].get(x, {}) for x in ATTRS}}
    ident = verify.identity_checks(generated)
    has_anchor = "13104" in gen["pop"].get("2025", {}).get("inflow", {})
    anchors = verify.anchors_check(generated) if has_anchor else {"passed": False, "note": "13104/2025 absent"}
    lang_ok, lang_checked = True, 0
    for slug in SLUGS:
        ref = None
        for lang in LANGS:
            p = site / lang / f"{slug}.html"
            if p.exists():
                obj = render.read_const_json(p.read_text(encoding="utf-8"), "POP_DATA")
                num = json.dumps(obj, sort_keys=True)
                ref = ref or num
                lang_checked += 1
                lang_ok &= (num == ref) and (obj == render.region_pop(gen["pop"], slug, region_of))
    # national: PREF_ALL verified (not replaced); boundaries compared with current polygons
    official_net = national.pref_net_from_extracts(merged)
    pref_check = (national.check_pref_all(pref_all_ref, official_net) if pref_all_ref
                  else {"passed": False, "note": "PREF_ALL not readable"})
    boundary = national.boundary_report(gen["pop"], polygon_codes)
    write_json(out / "boundary_report.json", boundary)
    write_json(out / "pref_all_check.json", pref_check)
    cov = {y: {d: len(cs) for d, cs in dd.items()} for y, dd in gen["pop"].items()}
    manifests = {str(m["year"]): m for m in (read_json(p) for d in a.extracts for p in Path(d).rglob("manifest_*.json"))}
    gi = gen["issues"]
    blockers = []
    for y in expected_years:
        m = manifests.get(y)
        if not m:
            blockers.append(f"{y}: extraction manifest missing")
        elif not m.get("complete"):
            blockers.append(f"{y}: extraction not complete (files={len(m['files'])}/{m.get('expected_files')}, "
                            f"listing_problems={m.get('listing_problems')}, discovery_failures={len(m['discovery_failures'])})")
        for d in ("inflow", "outflow"):
            if not cov.get(y, {}).get(d):
                blockers.append(f"{y}/{d}: no generated records")
    for name, ok in (("identity checks", ident["passed"]), ("anchors", anchors.get("passed")),
                     ("languages identical and equal to data", lang_ok)):
        if not ok:
            blockers.append(f"{name} failed")
    if pages_written != len(LANGS) * len(SLUGS):
        blockers.append(f"regional pages written {pages_written}/{len(LANGS) * len(SLUGS)}")
    i18n_bad = []
    for m in i18n_matrix:
        dd = json.loads((site / m["folder"] / "i18n" / f"{m['code']}.json").read_text(encoding="utf-8"))
        for k in ("no_published_data", "ranking_unavailable", "ranking_note", "age_unknown_note"):
            if dd.get(k) != render.MESSAGES[k][m["code"]]:
                i18n_bad.append(f"{m['folder']}/{m['code']}:{k}")
        if "foreign_2019_unavailable" in dd:
            i18n_bad.append(f"{m['folder']}/{m['code']}:foreign_2019_unavailable still present")
    if i18n_bad:
        blockers.append(f"translation dictionary content wrong: {i18n_bad[:5]}")
    if len(i18n_matrix) != len(LANGS) * len(LANGS):
        blockers.append(f"translation dictionaries written {len(i18n_matrix)}/{len(LANGS) * len(LANGS)}")
    if national_written != len(LANGS):
        blockers.append(f"national pages written {national_written}/{len(LANGS)}")
    if issues["pages"] or issues["slug_map_mismatch"] or issues["national"]:
        blockers.append("page/i18n/national issues present")
    for key in ("duplicate_destination", "cross_table_total_mismatch", "integrity", "sheet_failures", "file_failures"):
        if gi[key]:
            blockers.append(f"{key}: {len(gi[key])}")
    summary = {"generated_at": utcnow(), "ranking_tier": gen["tier"], "coverage_records": cov,
               "issue_counts": {k: len(v) for k, v in gi.items()}, "page_issues": issues,
               "pages_written": pages_written, "pages_expected": len(LANGS) * len(SLUGS),
               "national_pages_written": national_written,
               "search_index_entries": len(new_index),
               "i18n_matrix_files": len(i18n_matrix),
               "i18n_matrix_replacing_different_production_variants": [
                   f"{m['folder']}/{m['code']}" for m in i18n_matrix if m["production_differs_from_canonical"]],
               "i18n_matrix_new_files": [f"{m['folder']}/{m['code']}" for m in i18n_matrix
                                         if not m["production_file_existed"]],
               "identity": {k: v for k, v in ident.items() if k != "failures"},
               "identity_failures": ident["failures"][:50], "anchors": anchors,
               "languages_identical_and_equal_to_data": lang_ok, "language_pages_checked": lang_checked,
               "pref_all_check": {k: v for k, v in pref_check.items() if k not in ("mismatches", "not_checkable")},
               "boundary": {k: v for k, v in boundary.items() if k != "years"} | {"years": boundary.get("years", {})},
               "blockers": blockers, "complete": not blockers}
    write_json(out / "build_summary.json", summary)
    write_json(out / "issues.json", gi)
    with zipfile.ZipFile(out / "deploy-candidate.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted((out / "site").rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out / "site"))
    print(json.dumps({"complete": summary["complete"], "blockers": blockers[:10],
                      "pages_written": pages_written, "national_pages_written": national_written,
                      "pref_all_passed": pref_check.get("passed")}, ensure_ascii=False))
    return 0


def cmd_cards(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    merged, _ = build.merge_extracts(_load_extracts(a.extracts))
    official = national.pref_net_from_extracts(merged)
    res = cards.check_cards(Path(a.cards), official, out / "site")
    write_json(out / "cards_report.json", res)
    L = ["# Regional data card check (5 languages)", "",
         f"Checked values: {res['checked_values']}, mismatches: {res['mismatch_count']}, "
         f"cards not checked: {res['cards_not_checked'] or 'none'}", "",
         "| lang | region | year | card | official (sum of prefecture net migration) | match |", "|---|---|---|---:|---:|---|"]
    for r in res["mismatches"][:120] or []:
        L.append(f"| {r['lang']} | {r['slug']} | {r['year']} | {r['card']:+,} | {r['official'] if r['official'] is None else format(r['official'], '+,')} | NO |")
    L += ["", f"Official region sums by year (should be 0 in total): {res['official_region_sum_total_by_year']}",
          f"Candidates written (work copies only): {res['candidates_written'] or 'none'}",
          f"Locations: {res['locations']}"]
    (out / "cards_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("checked_values", "mismatch_count", "cards_not_checked",
                                          "candidates_written", "passed")}, ensure_ascii=False))
    return 0


# ------------------------------------------------------------- verify-year

def cmd_verify_year(a):
    year = int(a.year)
    cand = Path(a.candidate)
    g = {k: _gz_read(cand / "json" / f"{k}.json.gz") for k in ("total", "nationality", "sex", "age", "age_unknown")}
    gen_y = {"total": {str(year): g["total"].get(str(year), {})},
             "age_unknown": {str(year): g["age_unknown"].get(str(year), {})}}
    for x in ATTRS:
        gen_y[x] = {str(year): g[x].get(str(year), {})}
    man = read_json(Path(a.manifest))
    files, master, not_verified = [], set(), []
    for f in man["files"]:
        path = Path(a.work) / "official" / f["official_file"] if f.get("official_file") else None
        if f["status"] != "completed":
            not_verified.append({"stem": f["stem"], "reason": f"status {f['status']}"})
        elif path is None or not path.exists():
            not_verified.append({"stem": f["stem"], "reason": "completed in manifest but official file not found"})
        else:
            files.append({"year": year, "kind": f["kind"], "direction": f["direction"], "path": path})
            master |= {c for c in verify.sheet_codes(path) if verify._is_dest(c)}
    expected = a.expected_files
    res = verify.verify(files, gen_y, {str(year): master})
    scope_ok = len(files) == expected and not not_verified and not man.get("scope_filtered")
    res.update({"year": year, "files_verified": len(files), "expected_files": expected,
                "files_not_verified": not_verified, "verification_checks_passed": res["passed"],
                "scope_complete": scope_ok, "passed": bool(res["passed"] and scope_ok),
                "generated_at": utcnow()})
    write_json(Path(a.out) / f"verification_{year}.json", res)
    print(json.dumps({k: res[k] for k in ("year", "checks", "rankings_values_checked", "failure_count",
                                          "verification_checks_passed", "scope_complete", "passed",
                                          "files_verified")}))
    return 0


def cmd_finalize(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = read_json(Path(a.candidate) / "build_summary.json")
    ver = {}
    for p in sorted(Path(a.verifications).rglob("verification_*.json")):
        v = read_json(p)
        ver[str(v["year"])] = v
    expected = [y for y in a.expected_years.split(",") if y]
    missing = [y for y in expected if y not in ver]
    blockers = list(summary.get("blockers", []))
    blockers += [f"{y}: no verification result" for y in missing]
    for y, v in ver.items():
        if not v.get("passed"):
            blockers.append(f"{y}: verification not passed (checks_passed={v.get('verification_checks_passed')}, "
                            f"scope_complete={v.get('scope_complete')}, files {v.get('files_verified')}/"
                            f"{v.get('expected_files')}, not verified={len(v.get('files_not_verified', []))})")
    pref = summary.get("pref_all_check", {})
    boundary = summary.get("boundary", {})
    open_items = []
    if not pref.get("passed"):
        open_items.append(f"PREF_ALL (prefecture net migration) not confirmed against official prefecture totals "
                          f"(checked={pref.get('checked')}, mismatches={pref.get('mismatch_count')}, "
                          f"not checkable={pref.get('not_checkable_count')})")
    if not boundary.get("polygons_available"):
        open_items.append("boundary correspondence not checked (geometry unavailable)")
    cr = Path(a.candidate) / "cards_report.json"
    if cr.exists():
        c = read_json(cr)
        if not c.get("passed"):
            open_items.append(f"regional cards: {c['mismatch_count']} value(s) differ from official sums; "
                              f"candidates for {c['candidates_written']} (work copies); not checked: {c['cards_not_checked']}")
    else:
        open_items.append("regional cards: check not run")
    open_items.append("static article text outside POP_DATA / cards is NOT regenerated or verified")
    open_items.append("in-browser rendering of the corrected pages was not part of this automated run")
    verified = bool(ver) and not blockers
    final = {"generated_at": utcnow(), "build_complete": summary["complete"],
             "overall_verified": verified, "fully_complete": False,
             "fully_complete_reason": "cards / in-browser check remain open" if verified else "blockers present",
             "blockers": blockers, "open_items": open_items,
             "verification": {y: {k: v.get(k) for k in ("checks", "rankings_values_checked", "failure_count",
                                                       "verification_checks_passed", "scope_complete", "passed",
                                                       "files_verified", "expected_files")} for y, v in ver.items()},
             "years_without_verification": missing}
    write_json(out / "verification_final.json", final)
    L = ["# Verification summary", "",
         f"Overall verified: **{verified}**  |  fully complete: **False** ({final['fully_complete_reason']})", ""]
    for y, v in sorted(ver.items()):
        L.append(f"- {y}: checks={v['checks']}, ranking values={v['rankings_values_checked']}, "
                 f"failures={v['failure_count']}, files {v['files_verified']}/{v['expected_files']}, passed={v['passed']}")
    L += [f"- years without verification result: {missing or 'none'}", "", "## Blockers"]
    L += [f"- {x}" for x in blockers] or ["- none"]
    L += ["", "## Open items (not verified)"] + [f"- {x}" for x in open_items]
    (out / "VERIFICATION.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0


def cmd_preview(a):
    """Local preview tree: candidate pages/data + current geometry, served as /maps/... (no deployment)."""
    cand, pages, out = Path(a.candidate), Path(a.pages), Path(a.out)
    shutil.copytree(cand / "site" / "maps" / "population", out / "maps" / "population", dirs_exist_ok=True)
    if (cand / "site" / "cards").exists():
        shutil.copytree(cand / "site" / "cards", out / "cards", dirs_exist_ok=True)
    for s in SLUGS:
        src = pages / "geo" / f"{s}.geojson"
        if src.exists():
            (out / "maps" / "shared").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out / "maps" / "shared" / f"{s}.geojson")
    print(f"preview tree: {out}  (serve this directory at the web root)")
    return 0


def add_full_parser(sub):
    pv = sub.add_parser("preview")
    pv.add_argument("--candidate", required=True); pv.add_argument("--pages", required=True)
    pv.add_argument("--out", required=True); pv.set_defaults(fn=cmd_preview)
    e = sub.add_parser("extract")
    e.add_argument("--year", required=True); e.add_argument("--work", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--only-pref", default=""); e.add_argument("--max-prefectures", type=int, default=0)
    e.add_argument("--deadline-minutes", type=float, default=0); e.set_defaults(fn=cmd_extract)
    f = sub.add_parser("fetch-pages"); f.add_argument("--out", required=True); f.set_defaults(fn=cmd_fetch_pages)
    s = sub.add_parser("assemble")
    s.add_argument("--extracts", nargs="+", required=True); s.add_argument("--snapshot", required=True)
    s.add_argument("--pages", required=True); s.add_argument("--out", required=True)
    s.add_argument("--expected-years", default="2019,2021,2025"); s.set_defaults(fn=cmd_assemble)
    c = sub.add_parser("cards")
    c.add_argument("--extracts", nargs="+", required=True); c.add_argument("--cards", required=True)
    c.add_argument("--out", required=True); c.set_defaults(fn=cmd_cards)
    v = sub.add_parser("verify-year")
    v.add_argument("--year", required=True); v.add_argument("--candidate", required=True)
    v.add_argument("--manifest", required=True); v.add_argument("--work", required=True)
    v.add_argument("--out", required=True); v.add_argument("--expected-files", type=int, default=EXPECTED_FILES_PER_YEAR)
    v.set_defaults(fn=cmd_verify_year)
    z = sub.add_parser("finalize")
    z.add_argument("--candidate", required=True); z.add_argument("--verifications", required=True)
    z.add_argument("--out", required=True); z.add_argument("--expected-years", default="2019,2021,2025")
    z.set_defaults(fn=cmd_finalize)
