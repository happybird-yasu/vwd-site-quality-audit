#!/usr/bin/env python3
"""Build the fixed public-article snapshot from an existing crawl checkpoint
(public pages only; the homepage is excluded; no secrets, no auth'd raw data, no drafts).
The checkpoint's _text was extracted by run_audit.extract_main_text at commit d52e877."""
import argparse
import hashlib
import json
import re
import sys

EXTRACTION = {
    "method": "run_audit.extract_main_text",
    "version": "d52e877f2b0c33b61b3e9605ed97e5221747a419",
    "description": "article>main>body; script/style/nav/header/footer and .vwd-article-footer/.mm-wrap/.vwd-library removed; whitespace collapsed",
}


def sha(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def build(checkpoint, source_meta, fetched_at):
    arts = []
    for p in checkpoint["pages"]:
        if p["theme"] == "homepage" or p["url"].rstrip("/") == "https://app-navi.biz":
            continue
        if not p.get("_text"):
            raise ValueError(f"empty text: {p['url']}")
        m = re.search(r"(20\d\d)", p.get("slug") or "")
        arts.append({"url": p["url"], "slug": p.get("slug"), "language": p["language"], "theme": p["theme"],
                     "region": p.get("region"), "year": int(m.group(1)) if m else None, "title": p.get("title"),
                     "fetched_at": fetched_at, "status_code": p.get("status_code"),
                     "char_count": len(p["_text"]), "text": p["_text"], "text_sha256": sha(p["_text"])})
    arts.sort(key=lambda r: r["url"])
    urls = [a["url"] for a in arts]
    if len(set(urls)) != len(urls):
        raise ValueError("duplicate urls")
    sid = hashlib.sha256("\n".join(a["url"] + a["text_sha256"] for a in arts).encode()).hexdigest()[:16]
    return {"snapshot_id": sid, "created_at": fetched_at, "article_count": len(arts), "source": source_meta,
            "extraction": EXTRACTION, "articles": arts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fetched-at", required=True)
    ap.add_argument("--source-run", default="42014955715")
    a = ap.parse_args()
    with open(a.checkpoint, encoding="utf-8") as f:
        cp = json.load(f)
    snap = build(cp, {"kind": "public crawl checkpoint of GitHub run " + a.source_run,
                      "note": "fetched_at is the run's completion time (per-page time not recorded in the checkpoint)"},
                 a.fetched_at)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    print(snap["snapshot_id"], snap["article_count"])


if __name__ == "__main__":
    sys.exit(main())
