#!/usr/bin/env python3
"""
ProbeProductList.py  --  READ-ONLY Cin7 diagnostic.

Checks whether Cin7's product LIST endpoint can feed the uplift guard in bulk,
so we can avoid one fetch per product. It answers:

  1. Does IncludeSuppliers=true reduce the max page size (1000 -> lower)?
  2. Do products that HAVE a supplier return their Cost in the list response?
  3. Are PriceTier10 + AdditionalAttribute2 present per product?
  4. Roughly how long would a full catalogue page-through take?

It makes only GET requests and writes NOTHING. Reads credentials from
C:\\Python\\Credentials.txt (same file the main script uses).

Usage:
    python ProbeProductList.py                # scans pages to find a supplier
    python ProbeProductList.py MER-B0040BBR   # also probes one SKU you know has a cost
"""

import os
import sys
import json
import time
import requests

CRED_PATH          = r"C:\Python\Credentials.txt"
PRODUCT_URL        = "https://inventory.dearsystems.com/ExternalApi/v2/product"
RATE_LIMIT_PER_MIN = 55          # used only for the time estimate
REQUEST_LIMIT      = 1000        # page size we ask for
PAUSE              = 1.2         # polite gap between calls (seconds)


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


_creds  = load_credentials()
_acct   = _creds.get("CIN7_ACCOUNT_ID", "")
_appkey = _creds.get("CIN7_APPLICATION_KEY", "")
if not _acct or not _appkey:
    sys.exit("Missing CIN7_ACCOUNT_ID / CIN7_APPLICATION_KEY in Credentials.txt")

HEADERS = {
    "api-auth-accountid":      _acct,
    "api-auth-applicationkey": _appkey,
    "Content-Type":            "application/json",
}


def fetch_page(page, limit, include_suppliers, sku=None):
    """One GET against the product list (or a single SKU). Returns (resp, seconds)."""
    params = {
        "Page": page,
        "Limit": limit,
        "IncludeSuppliers": "true" if include_suppliers else "false",
    }
    if sku:
        params["Sku"] = sku
    t0 = time.time()
    r = requests.get(PRODUCT_URL, headers=HEADERS, params=params, timeout=120)
    return r, time.time() - t0


def parse(r):
    """Return (products_list, total) or (None, None) on a non-JSON body."""
    try:
        body = r.json()
    except ValueError:
        return None, None
    if isinstance(body, dict):
        return body.get("Products", []), body.get("Total")
    if isinstance(body, list):
        return body, None
    return None, None


def die_http(label, r):
    sys.exit(f"  {label}: HTTP {r.status_code} - {r.text[:300]}")


NEEDED = ["PriceTier10", "AdditionalAttribute2", "AverageCost", "Suppliers"]


def field_report(p):
    return ", ".join(f"{k}={'present' if k in p else 'MISSING'}" for k in NEEDED)


print("=" * 66)
print("Cin7 product-list probe  (READ-ONLY -- makes no changes)")
print("=" * 66)

# --- [1] page size WITHOUT suppliers ------------------------------------------
print(f"\n[1] Limit={REQUEST_LIMIT}, IncludeSuppliers=false ...")
r0, dt0 = fetch_page(1, REQUEST_LIMIT, False)
if not (200 <= r0.status_code < 300):
    die_http("plain list", r0)
prods0, total0 = parse(r0)
if prods0 is None:
    sys.exit(f"  Non-JSON response: {r0.text[:300]}")
print(f"    HTTP {r0.status_code} | returned {len(prods0)} products | "
      f"Total in catalogue: {total0} | {dt0:.1f}s")

# --- [2] page size WITH suppliers ---------------------------------------------
print(f"\n[2] Limit={REQUEST_LIMIT}, IncludeSuppliers=true ...")
time.sleep(PAUSE)
r1, dt1 = fetch_page(1, REQUEST_LIMIT, True)
if not (200 <= r1.status_code < 300):
    die_http("list+suppliers", r1)
prods1, total1 = parse(r1)
if prods1 is None:
    sys.exit(f"  Non-JSON response: {r1.text[:300]}")
print(f"    HTTP {r1.status_code} | returned {len(prods1)} products | "
      f"Total: {total1} | {dt1:.1f}s")

# --- page-size verdict --------------------------------------------------------
print("\n--- Page size ---")
page_size = max(1, len(prods1))
if len(prods1) < len(prods0):
    print(f"    IncludeSuppliers REDUCES the page size: {len(prods0)} -> {len(prods1)} per page.")
    print(f"    (That raises the page count for a full pass -- see the estimate below.)")
else:
    print(f"    IncludeSuppliers does NOT reduce the page size ({len(prods1)} per page). Good.")

if prods1:
    print(f"\n    Fields on first product: {field_report(prods1[0])}")

# --- [3] find a product that actually has a supplier --------------------------
print("\n--- Supplier cost in the list response ---")
with_suppliers = []
examined = list(prods1)            # page 1 already in hand
page = 1
for _ in range(5):                 # scan up to 5 pages looking for suppliers
    for p in examined:
        if p.get("Suppliers"):
            with_suppliers.append(p)
    if with_suppliers:
        break
    if total1 is not None and page * page_size >= int(total1):
        break
    page += 1
    time.sleep(PAUSE)
    rp, _ = fetch_page(page, REQUEST_LIMIT, True)
    if not (200 <= rp.status_code < 300):
        print(f"    (page {page} returned HTTP {rp.status_code}; stopping scan)")
        break
    examined, _ = parse(rp)
    examined = examined or []

if with_suppliers:
    s = with_suppliers[0]
    rows = s.get("Suppliers") or []
    print(f"    Found {len(with_suppliers)} product(s) with suppliers in the scanned pages.")
    print(f"\n    Sample: SKU={s.get('SKU')!r}  Name={s.get('Name')!r}")
    print(f"      PriceTier10          = {s.get('PriceTier10')}")
    print(f"      AdditionalAttribute2 = {s.get('AdditionalAttribute2')!r}")
    print(f"      AverageCost          = {s.get('AverageCost')}")
    print(f"      Suppliers count      = {len(rows)}")
    print(f"      First supplier row (raw JSON):")
    print(json.dumps(rows[0], indent=10)[:1500])
    multi = sum(1 for p in with_suppliers if len(p.get('Suppliers') or []) > 1)
    print(f"\n    Of {len(with_suppliers)} with suppliers, {multi} have MORE than one supplier row.")
else:
    print("    No products with a populated Suppliers array in the pages scanned.")
    print("    Pass a SKU you know has a supplier cost as an argument to check it directly,")
    print("    e.g.  python ProbeProductList.py MER-B0040BBR")

# --- optional: probe one specific SKU -----------------------------------------
if len(sys.argv) > 1:
    sku = sys.argv[1].strip()
    print(f"\n--- Direct SKU probe: {sku!r} ---")
    time.sleep(PAUSE)
    rs, _ = fetch_page(1, REQUEST_LIMIT, True, sku=sku)
    if not (200 <= rs.status_code < 300):
        print(f"    HTTP {rs.status_code}: {rs.text[:200]}")
    else:
        ps, _ = parse(rs)
        if ps:
            p = ps[0]
            rows = p.get("Suppliers") or []
            print(f"    {field_report(p)}")
            print(f"    PriceTier10={p.get('PriceTier10')}  "
                  f"Attr2={p.get('AdditionalAttribute2')!r}  AverageCost={p.get('AverageCost')}")
            print(f"    Suppliers ({len(rows)}):")
            print(json.dumps(rows, indent=6)[:1500] if rows else "      (none)")
        else:
            print("    SKU not found.")

# --- [4] full page-through time estimate --------------------------------------
print("\n--- Full catalogue page-through estimate (IncludeSuppliers=true) ---")
if total1:
    pages        = -(-int(total1) // page_size)          # ceil
    by_latency   = pages * dt1
    by_ratelimit = pages / RATE_LIMIT_PER_MIN * 60.0
    slower       = max(by_latency, by_ratelimit)
    print(f"    {total1} products / {page_size} per page = {pages} page(s).")
    print(f"    ~{by_latency:.0f}s by response latency; ~{by_ratelimit:.0f}s held to "
          f"{RATE_LIMIT_PER_MIN}/min.")
    print(f"    Realistic full pass: ~{slower:.0f}s  (~{slower/60:.1f} min), brand size irrelevant.")
else:
    print("    Couldn't read Total, so no estimate.")

print("\n" + "=" * 66)
print("Done. Nothing was modified.")
print("=" * 66)
