import requests

creds = {}
with open("Credentials.txt", "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            creds[key.strip()] = value.strip()

SHOPIFY_STORE_URL    = creds.get("SHOPIFY_STORE_URL", "")
SHOPIFY_ACCESS_TOKEN = creds.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_URL      = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01"

shopify_headers = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type":           "application/json",
    "Accept":                 "application/json"
}

HANDLE = "viva-skyair02-skylo-side-entry-air-gap-fill-valve-1-2-uk-brass-thread"

print(f"Looking up product by handle: {HANDLE}")
print()

r = requests.get(
    f"{SHOPIFY_API_URL}/products.json",
    headers=shopify_headers,
    params={"handle": HANDLE, "fields": "id,title,variants"},
    timeout=30
)
print(f"Status: {r.status_code}")
products = r.json().get("products", [])
if products:
    p = products[0]
    print(f"Product: {p.get('title')}")
    print(f"Variants:")
    for v in p.get("variants", []):
        print(f"  - ID: {v.get('id')} | SKU: '{v.get('sku')}' | Price: {v.get('price')}")
else:
    print("Product not found by handle")
