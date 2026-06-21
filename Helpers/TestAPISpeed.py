# ==============================================================================
# RHS Group Ltd — API Speed Test
# ==============================================================================
# Measures the response time of a single GET call to each API.
# Reads credentials from Credentials.txt in the same folder.
# Run this to identify which API is the bottleneck.
# ==============================================================================

import os
import time
import json
import requests

# ==============================================================================
# Load Credentials
# ==============================================================================

def load_credentials(filepath="Credentials.txt"):
    creds = {}
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Credentials.txt not found at '{filepath}'")
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            creds[key.strip()] = value.strip()
    return creds

creds = load_credentials("Credentials.txt")

CIN7_ACCOUNT_ID       = creds.get("CIN7_ACCOUNT_ID", "")
CIN7_APPLICATION_KEY  = creds.get("CIN7_APPLICATION_KEY", "")
SIMPRO_CLIENT_ID      = creds.get("SIMPRO_CLIENT_ID", "")
SIMPRO_CLIENT_SECRET  = creds.get("SIMPRO_CLIENT_SECRET", "")
SIMPRO_ACCESS_TOKEN   = creds.get("SIMPRO_ACCESS_TOKEN", "")
SHOPIFY_STORE_URL     = creds.get("SHOPIFY_STORE_URL", "")
SHOPIFY_ACCESS_TOKEN  = creds.get("SHOPIFY_ACCESS_TOKEN", "")

SIMPRO_BASE_URL = "https://mjryder.simprosuite.com"

# ==============================================================================
# Helpers
# ==============================================================================

def timed_get(label, url, headers=None, params=None):
    print(f"\n  Testing {label}...")
    print(f"  URL: {url}")
    try:
        start = time.perf_counter()
        r = requests.get(url, headers=headers, params=params, timeout=30)
        elapsed = time.perf_counter() - start
        print(f"  Status:   {r.status_code}")
        print(f"  Time:     {elapsed:.3f}s")
        if 200 <= r.status_code < 300:
            print(f"  Result:   OK")
        else:
            print(f"  Result:   FAILED — {r.text[:200]}")
        return elapsed
    except Exception as e:
        print(f"  Result:   ERROR — {e}")
        return None

def timed_post(label, url, headers=None, data=None):
    print(f"\n  Testing {label}...")
    print(f"  URL: {url}")
    try:
        start = time.perf_counter()
        r = requests.post(url, headers=headers, data=data, timeout=30)
        elapsed = time.perf_counter() - start
        print(f"  Status:   {r.status_code}")
        print(f"  Time:     {elapsed:.3f}s")
        if 200 <= r.status_code < 300:
            print(f"  Result:   OK")
        else:
            print(f"  Result:   FAILED — {r.text[:200]}")
        return elapsed, r
    except Exception as e:
        print(f"  Result:   ERROR — {e}")
        return None, None

# ==============================================================================
# Tests
# ==============================================================================

print("=" * 60)
print("RHS Group Ltd — API Speed Test")
print("=" * 60)

results = {}

# --- Cin7: GET a known product by SKU ---
print("\n[ 1 ] Cin7")
cin7_headers = {
    "api-auth-accountid":    CIN7_ACCOUNT_ID,
    "api-auth-applicationkey": CIN7_APPLICATION_KEY,
    "Content-Type":          "application/json",
    "Accept":                "application/json",
}
# Use a small product list call rather than a specific SKU
t = timed_get(
    "Cin7 — product list (1 result)",
    "https://inventory.dearsystems.com/ExternalApi/v2/product",
    headers=cin7_headers,
    params={"Page": 1, "Limit": 1}
)
results["Cin7 product list"] = t

# --- simPRO: token refresh ---
print("\n[ 2 ] simPRO — token refresh")
t2, token_resp = timed_post(
    "simPRO — OAuth token",
    f"{SIMPRO_BASE_URL}/oauth2/token",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    data={
        "grant_type":    "client_credentials",
        "client_id":     SIMPRO_CLIENT_ID,
        "client_secret": SIMPRO_CLIENT_SECRET,
    }
)
results["simPRO token refresh"] = t2

# --- simPRO: catalogue search ---
new_token = SIMPRO_ACCESS_TOKEN
if token_resp is not None and 200 <= token_resp.status_code < 300:
    try:
        new_token = token_resp.json().get("access_token", SIMPRO_ACCESS_TOKEN)
    except Exception:
        pass

print("\n[ 3 ] simPRO — catalogue search")
t3 = timed_get(
    "simPRO — catalogue search (1 result)",
    f"{SIMPRO_BASE_URL}/api/v1.0/companies/0/catalogs/",
    headers={
        "Authorization": f"Bearer {new_token}",
        "Content-Type":  "application/json",
    },
    params={"pageSize": 1}
)
results["simPRO catalogue search"] = t3

# --- Shopify: GraphQL product variant lookup ---
print("\n[ 4 ] Shopify")
shopify_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/graphql.json"
shopify_headers = {
    "Content-Type":          "application/json",
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
}
gql_payload = json.dumps({
    "query": "{ shop { name } }"
})
print(f"\n  Testing Shopify — shop name query...")
print(f"  URL: {shopify_url}")
try:
    start = time.perf_counter()
    r = requests.post(shopify_url, headers=shopify_headers, data=gql_payload, timeout=30)
    elapsed = time.perf_counter() - start
    print(f"  Status:   {r.status_code}")
    print(f"  Time:     {elapsed:.3f}s")
    if 200 <= r.status_code < 300:
        print(f"  Result:   OK")
    else:
        print(f"  Result:   FAILED — {r.text[:200]}")
    results["Shopify GraphQL"] = elapsed
except Exception as e:
    print(f"  Result:   ERROR — {e}")
    results["Shopify GraphQL"] = None

# --- Run each test 3 times for average ---
print("\n" + "=" * 60)
print("Repeat test x3 — Cin7 GET by SKU with IncludeSuppliers")
print("(This is the actual call made by PriceUpdateIncShopify)")
print("=" * 60)

# Use a real SKU from your catalogue
TEST_SKU = "KAR-SQR508C"

cin7_times = []
for i in range(3):
    t = timed_get(
        f"Cin7 SKU lookup run {i+1} ({TEST_SKU})",
        "https://inventory.dearsystems.com/ExternalApi/v2/product",
        headers=cin7_headers,
        params={"SKU": TEST_SKU, "IncludeSuppliers": "true"}
    )
    if t:
        cin7_times.append(t)

if cin7_times:
    avg = sum(cin7_times) / len(cin7_times)
    print(f"\n  Cin7 SKU lookup average: {avg:.3f}s")
    print(f"  Theoretical max SKUs/min: {60/avg:.0f}")

# ==============================================================================
# Summary
# ==============================================================================

print("\n" + "=" * 60)
print("Summary")
print("=" * 60)
for name, t in results.items():
    if t is not None:
        print(f"  {name:<35} {t:.3f}s")
    else:
        print(f"  {name:<35} FAILED")
print("=" * 60)
