#!/usr/bin/env python3
"""Exhaustive all-pairs full-text comparison (stdlib only; no API calls).

Metric definition (symmetric, order independent)
------------------------------------------------
difflib.SequenceMatcher(None, x, y, autojunk=False).ratio() is NOT symmetric
(ratio(x,y) != ratio(y,x) in general). To make results independent of input
order, every pair is canonicalised as (lo, hi) by URL string order, and BOTH
directions are computed:
    ratio_ab = SM(a, b).ratio(),  ratio_ba = SM(b, a).ratio()
    sym      = (ratio_ab + ratio_ba) / 2           <- the reported score
matched_ab / matched_ba = sum of matching block sizes in each direction.
No character cap: the whole body text is compared.

Cross-language string similarity is NOT a semantic duplicate signal.
"""
import argparse
import csv
import difflib
import gzip
import hashlib
import itertools
import json
import os
import signal
import sys
import time
from collections import Counter, defaultdict

METRIC_VERSION = "sm-sym-v1"
PAIR_TIMEOUT_SECONDS = 180

CATEGORIES = ("same_lang_same_theme", "same_lang_diff_theme",
              "diff_lang_same_theme", "diff_lang_diff_theme")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_snapshot(path):
    """Load snapshot, verify per-article hashes, return records sorted by URL."""
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    recs = sorted(snap["articles"], key=lambda r: r["url"])
    urls = [r["url"] for r in recs]
    if len(set(urls)) != len(urls):
        raise ValueError("duplicate URL in snapshot")
    for r in recs:
        if sha256(r["text"]) != r["text_sha256"]:
            raise ValueError(f"hash mismatch: {r['url']}")
    return snap, recs


def snapshot_identity(recs):
    """Identity bound to every result: snapshot content, metric and runtime version."""
    sid = hashlib.sha256("\n".join(r["url"] + r["text_sha256"] for r in sorted(recs, key=lambda r: r["url"]))
                         .encode()).hexdigest()[:16]
    return {"snapshot_id": sid, "article_count": len(recs), "metric_version": METRIC_VERSION,
            "python": "%d.%d" % sys.version_info[:2]}


IDENTITY_KEYS = ("snapshot_id", "article_count", "metric_version", "python")


class ManifestMismatch(Exception):
    pass


def check_manifest(path, identity, shard=None, shards=None, required=True):
    """Return None if OK, else a human-readable reason."""
    if not os.path.exists(path):
        return f"manifest missing: {path}" if required else None
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return f"manifest unreadable: {path}: {exc}"
    for k in IDENTITY_KEYS:
        if m.get(k) != identity[k]:
            return f"manifest mismatch {path}: {k} {m.get(k)!r} != {identity[k]!r}"
    if shard is not None and (m.get("shard") != shard or m.get("shards") != shards):
        return f"manifest mismatch {path}: shard {m.get('shard')}/{m.get('shards')} != {shard}/{shards}"
    return None


def pair_id(url_a, url_b):
    lo, hi = sorted((url_a, url_b))
    return hashlib.sha256((lo + "\n" + hi).encode("utf-8")).hexdigest()[:20]


def category(a, b):
    same_lang = a["language"] == b["language"]
    same_theme = a["theme"] == b["theme"]
    return ("same_lang" if same_lang else "diff_lang") + "_" + ("same_theme" if same_theme else "diff_theme")


def expected_pairs(recs):
    """Every unordered pair exactly once, canonical (lo, hi). Independent of input order."""
    recs = sorted(recs, key=lambda r: r["url"])
    for a, b in itertools.combinations(recs, 2):
        yield pair_id(a["url"], b["url"]), a, b


def shard_of(pid, shards):
    return int(pid, 16) % shards


def expected_pair_ids(recs):
    return {pid for pid, _, _ in expected_pairs(recs)}


def compare_texts(ta, tb):
    sm1 = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    r1 = sm1.ratio()
    m1 = sum(bl.size for bl in sm1.get_matching_blocks())
    sm2 = difflib.SequenceMatcher(None, tb, ta, autojunk=False)
    r2 = sm2.ratio()
    m2 = sum(bl.size for bl in sm2.get_matching_blocks())
    return r1, r2, m1, m2


def compare_pair(pid, a, b):
    r1, r2, m1, m2 = compare_texts(a["text"], b["text"])
    return {
        "pair_id": pid, "url_a": a["url"], "url_b": b["url"],
        "lang_a": a["language"], "lang_b": b["language"],
        "theme_a": a["theme"], "theme_b": b["theme"],
        "category": category(a, b),
        "len_a": len(a["text"]), "len_b": len(b["text"]),
        "ratio_ab": round(r1, 6), "ratio_ba": round(r2, 6),
        "sym": round((r1 + r2) / 2, 6),
        "matched_ab": m1, "matched_ba": m2,
        "status": "ok",
    }


class PairTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise PairTimeout()


def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # torn last line from an interrupted job: pair is simply redone
    return out


def run_shard(recs, shard, shards, out_dir, max_seconds=None, resume_dirs=(), compare=compare_pair,
              pair_timeout=PAIR_TIMEOUT_SECONDS):
    """Compare all pairs of one shard. Appends to out_dir/shard-XXX.jsonl, resumes from it
    and from any prior-run directories. Failures go to shard-XXX.failures.jsonl and are
    never counted as compared."""
    os.makedirs(out_dir, exist_ok=True)
    name = f"shard-{shard:03d}"
    path = os.path.join(out_dir, name + ".jsonl")
    fpath = os.path.join(out_dir, name + ".failures.jsonl")
    identity = snapshot_identity(recs)
    mpath = os.path.join(out_dir, name + ".manifest.json")
    # Reject (never silently reuse) results produced for another snapshot/metric/runtime/shard layout.
    for d in (out_dir, *resume_dirs):
        jl = os.path.join(d, name + ".jsonl")
        if os.path.exists(jl) and os.path.getsize(jl) > 0:
            why = check_manifest(os.path.join(d, name + ".manifest.json"), identity, shard, shards)
            if why:
                raise ManifestMismatch(why)
    done = set()
    for d in (out_dir, *resume_dirs):
        for r in read_jsonl(os.path.join(d, name + ".jsonl")):
            if r.get("status") == "ok":
                done.add(r["pair_id"])
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({**identity, "shard": shard, "shards": shards}, f)
    # carry over resumed results into this out_dir so the new artifact is self-contained
    if resume_dirs:
        have = {r["pair_id"] for r in read_jsonl(path)}
        with open(path, "a", encoding="utf-8") as f:
            for d in resume_dirs:
                if os.path.abspath(d) == os.path.abspath(out_dir):
                    continue
                for r in read_jsonl(os.path.join(d, name + ".jsonl")):
                    if r.get("status") == "ok" and r["pair_id"] not in have:
                        have.add(r["pair_id"])
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    todo = [(pid, a, b) for pid, a, b in expected_pairs(recs) if shard_of(pid, shards) == shard and pid not in done]
    start = time.time()
    stats = {"shard": shard, "shards": shards, "todo_at_start": len(todo), "already_done": len(done),
             "compared_now": 0, "errors": 0, "timeouts": 0, "stopped_by_budget": False}
    use_alarm = hasattr(signal, "SIGALRM")
    if use_alarm:
        signal.signal(signal.SIGALRM, _alarm)
    with open(path, "a", encoding="utf-8") as f, open(fpath, "a", encoding="utf-8") as ff:
        for pid, a, b in todo:
            if max_seconds is not None and time.time() - start > max_seconds:
                stats["stopped_by_budget"] = True
                break
            try:
                if use_alarm:
                    signal.alarm(pair_timeout)
                rec = compare(pid, a, b)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                stats["compared_now"] += 1
            except PairTimeout:
                stats["timeouts"] += 1
                ff.write(json.dumps({"pair_id": pid, "url_a": a["url"], "url_b": b["url"],
                                     "status": "timeout"}) + "\n")
            except Exception as exc:  # noqa: BLE001 - record and continue, never silently pass
                stats["errors"] += 1
                ff.write(json.dumps({"pair_id": pid, "url_a": a["url"], "url_b": b["url"],
                                     "status": "error", "error": f"{type(exc).__name__}: {exc}"}) + "\n")
            finally:
                if use_alarm:
                    signal.alarm(0)
            if stats["compared_now"] % 200 == 0:
                f.flush()
                print(f"[shard {shard}] {stats['compared_now']}/{len(todo)} {round(time.time()-start)}s", flush=True)
    stats["elapsed_seconds"] = round(time.time() - start, 1)
    with open(os.path.join(out_dir, name + ".stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    return stats


def verify_completeness(recs, results):
    """Exact-set check between expected pair ids and actual ok results."""
    exp = expected_pair_ids(recs)
    ok_ids = [r["pair_id"] for r in results if r.get("status") == "ok"]
    cnt = Counter(ok_ids)
    actual = set(cnt)
    dup = sorted(p for p, c in cnt.items() if c > 1)
    missing = sorted(exp - actual)
    extra = sorted(actual - exp)
    return {"expected": len(exp), "actual_unique_ok": len(actual), "duplicates": len(dup),
            "missing": len(missing), "extra": len(extra),
            "missing_sample": missing[:20], "extra_sample": extra[:20], "duplicate_sample": dup[:20],
            "exact_match": not dup and not missing and not extra}


def collect_results(dirs, identity=None, shards=None):
    """Returns (results, failures, conflicts, info). info reports raw line/duplicate counts and
    manifest problems; identical duplicates are counted (not hidden), conflicts are failures."""
    results = {}
    conflicts = []
    failures = []
    info = {"raw_ok_lines": 0, "identical_duplicates": 0, "manifest_errors": [], "shard_files": 0}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".failures.jsonl"):
                failures.extend(read_jsonl(os.path.join(d, fn)))
            elif fn.startswith("shard-") and fn.endswith(".jsonl"):
                info["shard_files"] += 1
                if identity is not None:
                    n = int(fn[6:9])
                    why = check_manifest(os.path.join(d, fn[:-6] + ".manifest.json"), identity, n, shards)
                    if why:
                        info["manifest_errors"].append(why)
                        continue  # results of a mismatched/unknown run are not accepted
                for r in read_jsonl(os.path.join(d, fn)):
                    if r.get("status") != "ok":
                        continue
                    info["raw_ok_lines"] += 1
                    if r["pair_id"] in results:
                        if results[r["pair_id"]] != r:
                            conflicts.append(r)
                        else:
                            info["identical_duplicates"] += 1
                        continue
                    results[r["pair_id"]] = r
    return list(results.values()), failures, conflicts, info


def validate_results(recs, results):
    """Each result must agree with the snapshot: URL pair, ids, category, lengths, ranges."""
    by = {r["url"]: r for r in recs}
    bad = []
    for r in results:
        a, b = by.get(r.get("url_a")), by.get(r.get("url_b"))
        why = None
        if a is None or b is None:
            why = "unknown url"
        elif not r["url_a"] < r["url_b"]:
            why = "non-canonical order"
        elif r["pair_id"] != pair_id(r["url_a"], r["url_b"]):
            why = "pair_id != hash(urls)"
        elif r["category"] != category(a, b):
            why = "category mismatch"
        elif (r["len_a"], r["len_b"]) != (len(a["text"]), len(b["text"])):
            why = "length mismatch"
        elif (r["lang_a"], r["lang_b"], r["theme_a"], r["theme_b"]) != (a["language"], b["language"], a["theme"], b["theme"]):
            why = "lang/theme mismatch"
        elif not (0 <= r["sym"] <= 1 and r["matched_ab"] <= r["len_a"] and r["matched_ba"] <= r["len_b"]):
            why = "value out of range"
        if why:
            bad.append({"pair_id": r.get("pair_id"), "reason": why})
    return bad


def page_unique_stats(recs, results):
    """Whole-text coverage per page against its best same-language same-theme peer.
    Reference value only: shared definitions/boilerplate lower it without implying poor content."""
    by_url = {r["url"]: r for r in recs}
    best = {}
    for r in results:
        if r["category"] != "same_lang_same_theme":
            continue
        for me, other, matched, ln in ((r["url_a"], r["url_b"], r["matched_ab"], r["len_a"]),
                                       (r["url_b"], r["url_a"], r["matched_ba"], r["len_b"])):
            cov = matched / ln if ln else 0.0
            cur = best.get(me)
            if (cur is None or r["sym"] > cur["max_sym_same_lang_theme"]
                    or (r["sym"] == cur["max_sym_same_lang_theme"] and other < cur["peer"])):
                best[me] = {"url": me, "peer": other, "max_sym_same_lang_theme": r["sym"],
                            "coverage_by_best_peer": round(cov, 4)}
    out = []
    for u, rec in by_url.items():
        b = best.get(u)
        row = {"url": u, "language": rec["language"], "theme": rec["theme"], "chars": len(rec["text"])}
        if b:
            row.update(b)
            row["unique_ratio_full_text_ref"] = round(1 - b["coverage_by_best_peer"], 4)
        else:
            row.update({"peer": None, "max_sym_same_lang_theme": None, "coverage_by_best_peer": None,
                        "unique_ratio_full_text_ref": None})
        out.append(row)
    return out


def quantiles(vals):
    if not vals:
        return {}
    v = sorted(vals)
    n = len(v)
    q = lambda p: v[min(n - 1, int(p * n))]
    return {"n": n, "min": v[0], "p50": q(.5), "p90": q(.9), "p99": q(.99), "max": v[-1],
            "mean": round(sum(v) / n, 4)}


def aggregate(recs, dirs, out_dir, snapshot_meta=None, strict=True, shards=None):
    identity = snapshot_identity(recs)
    if shards is None:  # infer from manifests
        for d in dirs:
            for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                if fn.endswith(".manifest.json"):
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        shards = json.load(f).get("shards")
                    break
            if shards:
                break
    results, failures, conflicts, info = collect_results(dirs, identity, shards)
    invalid = validate_results(recs, results)
    results = [r for r in results if r["pair_id"] not in {x["pair_id"] for x in invalid}] if invalid else results
    comp = verify_completeness(recs, results)
    comp["raw_ok_result_lines"] = info["raw_ok_lines"]
    comp["raw_duplicate_lines_identical"] = info["identical_duplicates"]
    comp["failed_pairs_recorded"] = len(failures)
    comp["conflicting_duplicates"] = len(conflicts)
    comp["invalid_results"] = len(invalid)
    comp["invalid_sample"] = invalid[:20]
    comp["manifest_errors"] = info["manifest_errors"]
    comp["shards_expected"] = shards
    comp["shard_files_found"] = info["shard_files"]
    comp["identity"] = identity
    comp["exact_match"] = (comp["exact_match"] and not conflicts and not invalid and not info["manifest_errors"]
                           and not failures)
    os.makedirs(out_dir, exist_ok=True)
    cats = defaultdict(list)
    for r in results:
        cats[r["category"]].append(r)
    summary = {"metric_version": METRIC_VERSION, "snapshot": snapshot_meta, "completeness": comp,
               "articles": len(recs),
               "note_cross_language": "異言語ペアの文字列類似は意味的重複判定に使えない。参考表示のみ。",
               "categories": {}}
    for c in CATEGORIES:
        rows = sorted(cats.get(c, []), key=lambda r: (-r["sym"], r["pair_id"]))
        summary["categories"][c] = {
            "pairs": len(rows), "sym": quantiles([r["sym"] for r in rows]),
            "ge_0.6": sum(r["sym"] >= .6 for r in rows), "ge_0.8": sum(r["sym"] >= .8 for r in rows),
            "ge_0.9": sum(r["sym"] >= .9 for r in rows),
            "top20": [{k: r[k] for k in ("pair_id", "url_a", "url_b", "sym", "ratio_ab", "ratio_ba")} for r in rows[:20]],
        }
    asym = [abs(r["ratio_ab"] - r["ratio_ba"]) for r in results]
    summary["asymmetry_abs_diff"] = quantiles(asym)
    uniq = page_unique_stats(recs, results)
    summary["unique_ratio_full_text_ref"] = quantiles([u["unique_ratio_full_text_ref"] for u in uniq
                                                       if u["unique_ratio_full_text_ref"] is not None])
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "page_unique_stats.json"), "w", encoding="utf-8") as f:
        json.dump(uniq, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "failures.json"), "w", encoding="utf-8") as f:
        json.dump(failures, f, ensure_ascii=False, indent=1)
    cols = ["pair_id", "url_a", "url_b", "lang_a", "lang_b", "theme_a", "theme_b", "category", "len_a", "len_b",
            "ratio_ab", "ratio_ba", "sym", "matched_ab", "matched_ba"]
    with gzip.open(os.path.join(out_dir, "all_pairs.csv.gz"), "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(results, key=lambda r: r["pair_id"]):
            w.writerow([r[c] for c in cols])
    with open(os.path.join(out_dir, "completeness.json"), "w", encoding="utf-8") as f:
        json.dump(comp, f, ensure_ascii=False, indent=2)
    print(json.dumps(comp, ensure_ascii=False, indent=2))
    if strict and not comp["exact_match"]:
        print("INCOMPLETE: expected pair set != actual pair set. NOT a pass.", file=sys.stderr)
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shard")
    s.add_argument("--snapshot", required=True)
    s.add_argument("--shard", type=int, required=True)
    s.add_argument("--shards", type=int, required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--max-seconds", type=float, default=None)
    s.add_argument("--resume-dir", action="append", default=[])
    g = sub.add_parser("aggregate")
    g.add_argument("--snapshot", required=True)
    g.add_argument("--results", action="append", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--no-strict", action="store_true")
    g.add_argument("--shards", type=int, default=None)
    b = sub.add_parser("bench")
    b.add_argument("--snapshot", required=True)
    b.add_argument("--n", type=int, default=200)
    a = ap.parse_args()
    snap, recs = load_snapshot(a.snapshot)
    if a.cmd == "shard":
        st = run_shard(recs, a.shard, a.shards, a.out, a.max_seconds, a.resume_dir)
        print(json.dumps(st))
        return 0
    if a.cmd == "aggregate":
        meta = {k: snap.get(k) for k in ("snapshot_id", "created_at", "extraction", "article_count", "source")}
        return aggregate(recs, a.results, a.out, meta, strict=not a.no_strict, shards=a.shards)
    if a.cmd == "bench":
        import random
        rng = random.Random(1)
        pairs = list(expected_pairs(recs))
        sample = rng.sample(pairs, min(a.n, len(pairs)))
        t = time.time()
        for pid, x, y in sample:
            compare_pair(pid, x, y)
        el = time.time() - t
        per = el / len(sample)
        print(json.dumps({"sampled": len(sample), "sec_per_pair": per, "total_pairs": len(pairs),
                          "est_total_cpu_hours": per * len(pairs) / 3600}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
