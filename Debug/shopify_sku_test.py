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

# Test the product from the URL
HANDLE = "sp563-110mm-black-135-bend-ds"
TEST_SKU = "FLO-278322"

print(f"Looking up product by handle: {HANDLE}")
print()

# Fetch product by handle to see what SKU is stored
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

print()
print(f"Now testing GraphQL search for SKU: {TEST_SKU}")
query = {
    "query": '{ productVariants(first: 5, query: "sku:\\"' + TEST_SKU + '\\"") { edges { node { id sku price compareAtPrice product { title } } } } }'
}
r2 = requests.post(
    f"{SHOPIFY_API_URL}/graphql.json",
    headers=shopify_headers,
    json=query,
    timeout=30
)
edges = r2.json().get("data", {}).get("productVariants", {}).get("edges", [])
print(f"GraphQL results: {len(edges)}")
for e in edges:
    print(f"  - SKU: '{e['node'].get('sku')}' | ID: {e['node'].get('id')}")
