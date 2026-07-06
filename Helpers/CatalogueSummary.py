#!/usr/bin/env python3
"""
CatalogueSummary.py  --  READ-ONLY Cin7 catalogue visualiser.

Lists every Brand with its product count, every Category with its product
count, then totals — a quick way to see the shape of the catalogue. Also
writes Summary1.csv next to this script (columns: Type, Name, Quantity,
PercentOfCatalogue) so you can sort/filter it in Excel.

Data source: the NEWEST of the main script's two caches - catalogue_index.json
(slim) or export_cache.json (full records, written by export mode) - when it is
fresher than MAX_CACHE_AGE_HOURS (no API calls; the export cache is ~370MB so
allow a few seconds to load). Otherwise a fresh lightweight scan of the whole
catalogue (SKU/Name/Brand/Category only, no suppliers, ~1.5 min at 55
calls/min). Makes only GET requests and writes NOTHING.

Reads credentials from C:\\Python\\Credentials.txt (same file the main script
uses). Run from anywhere:

    python CatalogueSummary.py            # cache if fresh, else scan
    python CatalogueSummary.py --fresh    # force a fresh scan

After the results, press R + Enter to run again with a FRESH scan (ignores the
caches — useful right after bulk changes in Cin7), or just Enter to exit.
"""

import os
import re
import csv
import sys
import json
import time
from collections import Counter
from datetime import datetime

import requests

CRED_PATH           = r"C:\Python\Credentials.txt"
PRODUCT_URL         = "https://inventory.dearsystems.com/ExternalApi/v2/product"
RATE_LIMIT_PER_MIN  = 55
PAGE_LIMIT          = 1000
MAX_CACHE_AGE_HOURS = 24

# The main script's caches live one folder up from Helpers/.
_PARENT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATHS = [os.path.join(_PARENT, "catalogue_index.json"),
               os.path.join(_PARENT, "export_cache.json")]

# Sortable Excel-friendly output, written next to this script.
SUMMARY_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "Summary1.csv")


def load_credentials(path=CRED_PATH):
    if not os.path.exists(path):
        sys.exit(f"Credentials file not found: {path}")
    creds = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            creds[k.strip()] = v.strip()
    return creds


def _cache_generated(path):
    """Cheaply read a cache's 'generated' timestamp (it sits at the start of the
    JSON, so no need to load a ~370MB file just to compare ages)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(200)
        m = re.search(r'"generated"\s*:\s*"([^"]+)"', head)
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M") if m else None
    except Exception:
        return None


def products_from_cache():
    """Return (products, source_str) from the NEWEST of the main script's caches
    (catalogue_index.json or export_cache.json) when it is fresh enough, else
    (None, None). Using the newest means the summary always agrees with
    whatever scan ran last (e.g. an export you just did)."""
    candidates = []
    for path in CACHE_PATHS:
        if os.path.exists(path):
            built = _cache_generated(path)
            if built:
                candidates.append((built, path))
    if not candidates:
        return None, None
    built, path = max(candidates)          # newest cache wins
    age_hours = (datetime.now() - built).total_seconds() / 3600.0
    if age_hours > MAX_CACHE_AGE_HOURS:
        print(f"  Newest cache ({os.path.basename(path)}) is {age_hours:.0f}h old "
              f"(limit {MAX_CACHE_AGE_HOURS}h) — scanning fresh.")
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        age_str = f"{age_hours:.0f}h ago" if age_hours >= 1 else f"{age_hours*60:.0f} min ago"
        generated = data.get("generated", "")
        return data.get("products", []), f"{os.path.basename(path)} (built {generated}, {age_str})"
    except Exception as e:
        print(f"  Cache unreadable ({e}) — scanning fresh.")
        return None, None


def products_from_api():
    """Lightweight full-catalogue scan (no suppliers). Returns (products, source)."""
    creds  = load_credentials()
    acct   = creds.get("CIN7_ACCOUNT_ID", "")
    appkey = creds.get("CIN7_APPLICATION_KEY", "")
    if not acct or not appkey:
        sys.exit("Missing CIN7_ACCOUNT_ID / CIN7_APPLICATION_KEY in Credentials.txt")
    headers = {"api-auth-accountid": acct, "api-auth-applicationkey": appkey,
               "Content-Type": "application/json"}
    gap = 60.0 / RATE_LIMIT_PER_MIN
    out, page, total = [], 1, None
    while True:
        t0 = time.time()
        r = requests.get(PRODUCT_URL, headers=headers,
                         params={"Page": page, "Limit": PAGE_LIMIT}, timeout=60)
        if not (200 <= r.status_code < 300):
            sys.exit(f"Cin7 product list failed (page {page}): "
                     f"{r.status_code} - {r.text[:300]}")
        body = r.json()
        products = body.get("Products", []) if isinstance(body, dict) else (body or [])
        if isinstance(body, dict):
            total = body.get("Total", total)
        if not products:
            break
        out.extend(products)
        print(f"  Scanned page {page} ({len(out)} products)...", end="\r")
        if total is not None and page * PAGE_LIMIT >= int(total):
            break
        if len(products) < PAGE_LIMIT:
            break
        page += 1
        elapsed = time.time() - t0
        if elapsed < gap:
            time.sleep(gap - elapsed)
    print(" " * 50, end="\r")
    return out, "fresh API scan"


def print_table(title, counter, total_products):
    print(f"\n{title}")
    print("-" * 58)
    width = max((len(n) for n in counter), default=10)
    width = min(max(width, 10), 42)
    for name, qty in counter.most_common():
        pct = qty / total_products * 100 if total_products else 0
        print(f"  {name:<{width}}  {qty:>7,}  ({pct:4.1f}%)")


def main(force_fresh=False):
    print("=" * 58)
    print("Cin7 Catalogue Summary  (READ-ONLY -- makes no changes)")
    print("=" * 58)

    fresh = force_fresh or "--fresh" in [a.lower() for a in sys.argv[1:]]
    products, source = (None, None)
    if not fresh:
        products, source = products_from_cache()
    if products is None:
        products, source = products_from_api()

    total = len(products)
    print(f"\n  Source: {source}")
    print(f"  Products: {total:,}")

    brands     = Counter()
    categories = Counter()
    for p in products:
        brands[(p.get("Brand") or "").strip() or "(blank)"] += 1
        categories[(p.get("Category") or "").strip() or "(blank)"] += 1

    print_table("BRANDS (by product count)", brands, total)
    print_table("CATEGORIES (by product count)", categories, total)

    # Sortable CSV for Excel: one row per brand/category, filter on Type.
    try:
        with open(SUMMARY_CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Type", "Name", "Quantity", "PercentOfCatalogue"])
            for kind, counter in (("Brand", brands), ("Category", categories)):
                for name, qty in counter.most_common():
                    pct = round(qty / total * 100, 2) if total else 0
                    w.writerow([kind, name, qty, pct])
        print(f"\n  Written: {SUMMARY_CSV_PATH}")
    except PermissionError:
        print(f"\n  WARNING: could not write Summary1.csv — is it open in Excel?")

    print("\n" + "=" * 58)
    print(f"  Total brands:     {len(brands):,}"
          + ("   (incl. blank)" if "(blank)" in brands else ""))
    print(f"  Total categories: {len(categories):,}"
          + ("   (incl. blank)" if "(blank)" in categories else ""))
    print(f"  Total products:   {total:,}")
    print("=" * 58)
    print("Done. Nothing was modified.")


if __name__ == "__main__":
    force = False
    while True:
        main(force_fresh=force)
        print("\nPress R + Enter to run again with a FRESH scan (ignores caches),")
        print("or just Enter to exit:")
        try:
            again = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if not again.startswith("r"):
            break
        force = True
        print()
