#!/usr/bin/env python3
"""One-off read-only helper: fetch a fixed list of URLs (GET only, reusing
run_audit.py's fetch_with_retry/extract_main_text, same retry/delay
constants) and print each page's title + extracted main text to stdout, so
a human/Claude/ChatGPT reviewer can read actual page content without this
session fetching the live site directly. Not part of the audit pipeline;
delete once the manual review it supports is done.
"""
import sys
import time

from bs4 import BeautifulSoup

from run_audit import REQUEST_DELAY_SECONDS, extract_main_text, fetch_with_retry

URLS = [
    "https://app-navi.biz/fr/population-hokkaido-fr/",
    "https://app-navi.biz/en/population-national-en/",
    "https://app-navi.biz/es/population-national-es/",
    "https://app-navi.biz/de/population-national-de/",
    "https://app-navi.biz/fr/population-national-fr/",
    "https://app-navi.biz/de/population-chubu-hokuriku-de/",
    "https://app-navi.biz/de/population-chugoku-de/",
    "https://app-navi.biz/en/population-kanto-en/",
    "https://app-navi.biz/de/population-kanto-de/",
    "https://app-navi.biz/en/population-kinki-en/",
]

TEXT_PRINT_CAP = 2000

for i, url in enumerate(URLS, start=1):
    resp, err = fetch_with_retry(url)
    time.sleep(REQUEST_DELAY_SECONDS)
    print(f"\n===== [{i}/{len(URLS)}] {url} =====")
    if err or resp is None or resp.status_code != 200:
        print(f"FETCH FAILED: {err or resp.status_code}")
        continue
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    print(f"title: {title}")
    text = extract_main_text(soup)
    print(f"char_count: {len(text)}")
    print("text:")
    print(text[:TEXT_PRINT_CAP])
    if len(text) > TEXT_PRINT_CAP:
        print(f"... [truncated, {len(text) - TEXT_PRINT_CAP} more chars]")

sys.stdout.flush()
