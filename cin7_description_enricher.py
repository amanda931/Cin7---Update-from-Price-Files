#!/usr/bin/env python3
# ==============================================================================
# RHS Group Ltd — Cin7 Product Description Enricher (standalone batch port of
# the proven Zapier "Description Enrichment" code step)
#
# What it does, per product:
#   - GETs the live product from Cin7 (with attachments, to check for images)
#   - Calls OpenAI (web search enabled, strict JSON schema — IDENTICAL prompt
#     and schema to the Zapier step, so output style/quality match exactly)
#   - Applies the same two-lane local safety gates (exact-product lane and the
#     cautious generic/KPS Select commodity lane) before any write
#   - If DRY_RUN is False and the gates pass, PUTs: the description field,
#     search terms (AdditionalAttribute3-6), Barcode (when its own strict rules
#     pass, exact-product lane only), and AdditionalAttribute8="true" as the
#     completion flag
#   - Logs everything (including output-only competitor/image research) to
#     Logs/enrich_log_<timestamp>.csv
#
# Batch behaviour:
#   - Targets products matching BRAND_FILTER / CATEGORY_FILTER in Config.yaml
#     where AdditionalAttribute8 is not "true" AND the description is blank
#   - Caps each run at ENRICH_MAX_PRODUCTS (Config.yaml)
#   - Resumable by design: AdditionalAttribute8 marks completed products
#
# IMPORTANT — never run this at the same time as:
#   - a LIVE cin7_price_updater.py run (this script's PUT echoes back
#     AdditionalAttribute1-10 as read at GET time, so a concurrent price run's
#     multiplier/fixed-price writes could be reverted), or
#   - the Zapier enrichment Zap on the same products.
#
# Credentials (C:\Python\Credentials.txt — same file the main script uses):
#   CIN7_ACCOUNT_ID: ...
#   CIN7_APPLICATION_KEY: ...
#   OPENAI_API_KEY: ...          <- add this line for this script
#
# Usage:
#   python cin7_description_enricher.py            # honours DRY_RUN in Config
#   (set ENRICH_* keys and BRAND_FILTER/CATEGORY_FILTER in Config.yaml first)
# ==============================================================================

import os
import re
import csv
import sys
import json
import time
import requests
import html as html_lib
from datetime import datetime
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
    _UK_TZ = ZoneInfo("Europe/London")
except Exception:
    _UK_TZ = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH  = r"C:\Python\Credentials.txt"

CIN7_PRODUCT_URL     = "https://inventory.dearsystems.com/ExternalApi/v2/product"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

CATALOGUE_CACHE_PATH = os.path.join(SCRIPT_DIR, "catalogue_index.json")
EXPORT_CACHE_PATH    = os.path.join(SCRIPT_DIR, "export_cache.json")

# Hard-coded completion flag — matches the Zapier step, so the two systems
# recognise each other's completed products.
COMPLETION_FLAG_FIELD = "AdditionalAttribute8"
COMPLETION_FLAG_VALUE = "true"

# Set by init_runtime(); module import stays side-effect free for testing.
cin7_headers   = {}
openai_headers = {}
CFG = {}


# ==============================================================================
# SECTION 01 — Config / credentials / rate limiter
# ==============================================================================

def _parse_bool(value, default=True):
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return default


def _parse_float(value, default):
    try:
        m = re.search(r"-?\d+(\.\d+)?", str(value))
        return float(m.group(0)) if m else float(default)
    except Exception:
        return float(default)


def _load_kv_file(path):
    data = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip()
    return data


def load_config():
    raw = _load_kv_file(os.path.join(SCRIPT_DIR, "Config.yaml"))
    cfg = {
        "DRY_RUN":                     _parse_bool(raw.get("DRY_RUN", "True")),
        "RATE_LIMIT_PER_MIN":          int(_parse_float(raw.get("RATE_LIMIT_PER_MIN", "55"), 55)),
        "BRAND_FILTER":                raw.get("BRAND_FILTER", "").strip(),
        "CATEGORY_FILTER":             raw.get("CATEGORY_FILTER", "").strip(),
        "EXCLUDE_BATHROOM_BRANDS":     _parse_bool(raw.get("EXCLUDE_BATHROOM_BRANDS", "False")),
        "CATALOGUE_MAX_AGE_HOURS":     int(_parse_float(raw.get("CATALOGUE_MAX_AGE_HOURS", "24"), 24)),
        "ENRICH_MAX_PRODUCTS":         int(_parse_float(raw.get("ENRICH_MAX_PRODUCTS", "25"), 25)),
        "ENRICH_MODEL":                raw.get("ENRICH_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini",
        "ENRICH_DESCRIPTION_FIELD":    raw.get("ENRICH_DESCRIPTION_FIELD", "Description").strip() or "Description",
        "ENRICH_ONLY_IF_BLANK":        _parse_bool(raw.get("ENRICH_ONLY_IF_BLANK", "True")),
        "ENRICH_SEARCH_TERMS_MODE":    (raw.get("ENRICH_SEARCH_TERMS_MODE", "blank_only").strip().lower()
                                        if raw.get("ENRICH_SEARCH_TERMS_MODE", "blank_only").strip().lower()
                                        in ("blank_only", "overwrite") else "blank_only"),
        "ENRICH_MIN_CONFIDENCE":       _parse_float(raw.get("ENRICH_MIN_CONFIDENCE", "0.85"), 0.85),
        "ENRICH_BARCODE_ONLY_IF_BLANK": _parse_bool(raw.get("ENRICH_BARCODE_ONLY_IF_BLANK", "True")),
        "ENRICH_MIN_BARCODE_CONFIDENCE": _parse_float(raw.get("ENRICH_MIN_BARCODE_CONFIDENCE", "0.90"), 0.90),
        "ENRICH_MANUFACTURER_FIELD":   raw.get("ENRICH_MANUFACTURER_FIELD", "Brand").strip() or "Brand",
    }
    return cfg


def init_runtime():
    """Load config + credentials and build API headers. Called from main()."""
    global cin7_headers, openai_headers, CFG
    CFG = load_config()

    creds  = _load_kv_file(CRED_PATH)
    acct   = creds.get("CIN7_ACCOUNT_ID", "")
    appkey = creds.get("CIN7_APPLICATION_KEY", "")
    oai    = creds.get("OPENAI_API_KEY", "")
    if not acct or not appkey:
        sys.exit(f"Missing CIN7_ACCOUNT_ID / CIN7_APPLICATION_KEY in {CRED_PATH}")
    if not oai:
        sys.exit(f"Missing OPENAI_API_KEY in {CRED_PATH} — add a line: OPENAI_API_KEY: sk-...")

    cin7_headers = {
        "api-auth-accountid":      acct,
        "api-auth-applicationkey": appkey,
        "Content-Type":            "application/json",
        "Accept":                  "application/json",
    }
    openai_headers = {
        "Authorization": f"Bearer {oai}",
        "Content-Type":  "application/json",
    }


class RateLimiter:
    """Simple gap-based limiter for Cin7 calls (shared 55/min budget)."""
    def __init__(self, per_min):
        self.gap  = 60.0 / max(1, per_min)
        self.last = 0.0

    def wait(self):
        now = time.time()
        delta = now - self.last
        if delta < self.gap:
            time.sleep(self.gap - delta)
        self.last = time.time()


rate_limiter = RateLimiter(55)


def uk_now_string():
    if _UK_TZ is not None:
        return datetime.now(_UK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==============================================================================
# SECTION 02 — General helpers (ported verbatim from the Zapier step)
# ==============================================================================

def truthy(value):
    return str(value).strip().lower() in ["true", "yes", "1", "y"]


def clean(value, default=""):
    if value is None:
        return default
    return str(value)


def clean_text(value, max_len=5000):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > max_len:
        text = text[:max_len] + "... truncated ..."
    return text


def to_float_safe(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def derive_manufacturer_part_number_from_sku(sku):
    sku_text = str(sku or "").strip()
    if not sku_text:
        return ""
    if "-" not in sku_text:
        return sku_text
    prefix, remainder = sku_text.split("-", 1)
    if remainder.strip():
        return remainder.strip()
    return sku_text


def extract_product_from_cin7_response(product_data):
    if isinstance(product_data, dict) and "Products" in product_data:
        products = product_data.get("Products", [])
        if not products:
            raise ValueError("No product found in Cin7 Products response")
        return products[0]
    if isinstance(product_data, dict) and "ProductList" in product_data:
        products = product_data.get("ProductList", [])
        if not products:
            raise ValueError("No product found in Cin7 ProductList response")
        return products[0]
    if isinstance(product_data, list):
        if not product_data:
            raise ValueError("No product found in Cin7 list response")
        return product_data[0]
    if isinstance(product_data, dict):
        return product_data
    raise ValueError("Unexpected Cin7 product response format")


def get_cin7_product_by_sku(sku):
    rate_limiter.wait()
    response = requests.get(
        CIN7_PRODUCT_URL,
        headers=cin7_headers,
        params={"SKU": sku, "IncludeSuppliers": "false", "IncludeAttachments": "true"},
        timeout=30,
    )
    try:
        response_body = response.json()
    except Exception:
        response_body = response.text
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"Cin7 GET failed: {response.status_code} - {str(response_body)[:2000]}")
    return extract_product_from_cin7_response(response_body)


def get_product_attachments(product):
    if not isinstance(product, dict):
        return []
    for key in ["Attachments", "Attachment", "ProductAttachments", "Files", "Documents"]:
        value = product.get(key)
        if isinstance(value, list):
            return value
    return []


def attachment_looks_like_image(attachment):
    if not isinstance(attachment, dict):
        return False
    file_name = clean(attachment.get("FileName") or attachment.get("Name")
                      or attachment.get("Filename") or "").lower()
    content_type = clean(attachment.get("ContentType") or attachment.get("MimeType")
                         or attachment.get("Type") or "").lower()
    download_url = clean(attachment.get("DownloadUrl") or attachment.get("DownloadURL")
                         or attachment.get("Url") or attachment.get("URL") or "").lower()
    image_extensions = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    if any(file_name.endswith(ext) for ext in image_extensions):
        return True
    if content_type.startswith("image/"):
        return True
    if any(ext in download_url for ext in image_extensions):
        return True
    return False


def summarise_existing_image_attachments(product):
    attachments = get_product_attachments(product)
    image_attachments = [a for a in attachments if attachment_looks_like_image(a)]
    return {
        "attachment_count": len(attachments),
        "image_attachment_count": len(image_attachments),
        "has_existing_image_attachments": len(image_attachments) > 0,
    }


def normalise_simple_text_key(value):
    text = clean(value, "").strip().lower()
    text = html_lib.unescape(text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def weak_or_blocked_search_term(value):
    text = normalise_simple_text_key(value)
    blocked_exact_terms = {
        "", "other", "n a", "na", "none", "unknown", "misc", "miscellaneous",
        "tbc", "tbd", "favourite", "favorite", "best", "cheap", "quality",
        "popular", "product", "item", "standard", "general", "plumbing",
    }
    return text in blocked_exact_terms


def sanitise_search_term(term):
    text = clean(term, "").strip()
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = text.replace(",", " ")
    text = text.replace('"', "").replace("'", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > 35:
        text = text[:35].strip()
    if weak_or_blocked_search_term(text):
        return ""
    return text


def replaceable_search_term(value):
    return weak_or_blocked_search_term(value)


def choose_search_term_value(existing_value, proposed_value, mode):
    existing = clean(existing_value, "").strip()
    proposed = sanitise_search_term(proposed_value)
    if mode == "overwrite":
        return proposed
    if existing and not replaceable_search_term(existing):
        return existing
    return proposed


def count_useful_search_terms(*terms):
    useful, seen = [], set()
    for term in terms:
        cleaned = sanitise_search_term(term)
        key = normalise_simple_text_key(cleaned)
        if not cleaned or not key or key in seen:
            continue
        useful.append(cleaned)
        seen.add(key)
    return len(useful)


def blankish(value):
    text = str(value or "").strip()
    if not text:
        return True
    stripped = re.sub(r"<[^>]+>", "", text).strip()
    return stripped == ""


def safe_file_name_base(value, fallback="product-image"):
    text = clean(value, "").strip()
    if not text:
        text = fallback
    text = re.sub(r"[^A-Za-z0-9._ -]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-_ .")
    if not text:
        text = fallback
    text = re.sub(r"\.(jpg|jpeg|png|webp|gif)$", "", text, flags=re.IGNORECASE)
    return text


def get_first_existing_description(product):
    found = {}
    for field in ["Description", "LongDescription", "ProductDescription",
                  "WebDescription", "ShortDescription"]:
        if field in product:
            found[field] = clean(product.get(field, ""))
    return found


def get_product_manufacturer(product, manufacturer_field):
    configured_value = clean(product.get(manufacturer_field, "")).strip()
    if configured_value:
        return configured_value, manufacturer_field
    for fallback_field in ["Brand", "AdditionalAttribute1", "AdditionalAttribute2", "Category"]:
        fallback_value = clean(product.get(fallback_field, "")).strip()
        if fallback_value:
            return fallback_value, fallback_field
    return "", ""


# ==============================================================================
# SECTION 03 — Barcode helpers (ported verbatim)
# ==============================================================================

def normalise_barcode(value):
    return re.sub(r"\D", "", clean(value, ""))


def barcode_length_valid(barcode):
    return len(barcode) in [8, 12, 13, 14]


def gtin_check_digit_valid(barcode):
    barcode = normalise_barcode(barcode)
    if not barcode_length_valid(barcode):
        return False
    digits = [int(char) for char in barcode]
    check_digit = digits[-1]
    body = digits[:-1]
    total = 0
    for index, digit in enumerate(reversed(body), start=1):
        total += digit * 3 if index % 2 == 1 else digit
    return (10 - (total % 10)) % 10 == check_digit


def classify_barcode_type(barcode):
    barcode = normalise_barcode(barcode)
    return {8: "EAN-8", 12: "UPC-A", 13: "EAN-13", 14: "GTIN-14"}.get(len(barcode), "unknown")


def validate_barcode_candidate(value):
    barcode = normalise_barcode(value)
    length_valid = barcode_length_valid(barcode)
    check_digit_valid = gtin_check_digit_valid(barcode) if length_valid else False
    return {
        "barcode_value": barcode,
        "barcode_type": classify_barcode_type(barcode),
        "digits_only": barcode.isdigit() if barcode else False,
        "length_valid": length_valid,
        "check_digit_valid": check_digit_valid,
        "validated": bool(barcode and length_valid and check_digit_valid),
    }


def decide_barcode_update(product, parsed_barcode_research, barcode_field_name,
                          only_update_if_blank, min_barcode_confidence):
    existing_barcode = clean(product.get(barcode_field_name, "")).strip() if barcode_field_name else ""
    existing_barcode_normalised = normalise_barcode(existing_barcode)

    if not isinstance(parsed_barcode_research, dict):
        parsed_barcode_research = {}

    ai_barcode_found = parsed_barcode_research.get("barcode_found") is True
    ai_barcode_value_raw = clean(parsed_barcode_research.get("barcode_value", ""))
    ai_barcode_confidence = to_float_safe(parsed_barcode_research.get("confidence", 0), default=0)
    ai_safe_to_update = parsed_barcode_research.get("safe_to_update_cin7_barcode") is True
    ai_source_type = clean(parsed_barcode_research.get("barcode_source_type", "")).lower()
    ai_source_url = clean(parsed_barcode_research.get("barcode_source_url", ""))

    validation = validate_barcode_candidate(ai_barcode_value_raw)
    allowed_barcode_source_types = ["official_manufacturer", "official_manufacturer_pdf",
                                    "trusted_supplier", "mixed_sources"]
    reasons = []
    if not ai_barcode_found:
        reasons.append("OpenAI did not find a barcode.")
    if not ai_safe_to_update:
        reasons.append("OpenAI did not mark barcode as safe to update.")
    if ai_barcode_confidence < min_barcode_confidence:
        reasons.append(f"Barcode confidence {ai_barcode_confidence} is below required {min_barcode_confidence}.")
    if ai_source_type not in allowed_barcode_source_types:
        reasons.append(f"Barcode source type '{ai_source_type}' is not allowed.")
    if not validation.get("validated"):
        reasons.append("Barcode failed local Python validation/check digit.")
    if only_update_if_blank and existing_barcode_normalised:
        reasons.append("Existing Cin7 Barcode is not blank and only_update_barcode_if_blank=true.")

    would_update = (
        ai_barcode_found and ai_safe_to_update
        and ai_barcode_confidence >= min_barcode_confidence
        and ai_source_type in allowed_barcode_source_types
        and validation.get("validated") is True
        and (not only_update_if_blank or not existing_barcode_normalised)
    )
    if would_update:
        reasons.append("Barcode passed all update rules.")

    return {
        "existing_barcode": existing_barcode,
        "barcode_found": ai_barcode_found,
        "barcode_value": validation.get("barcode_value", ""),
        "barcode_type": validation.get("barcode_type", ""),
        "barcode_source_url": ai_source_url,
        "barcode_source_type": ai_source_type,
        "barcode_confidence": ai_barcode_confidence,
        "barcode_validated": validation.get("validated", False),
        "would_update_cin7_barcode": would_update,
        "barcode_update_reasons": reasons,
    }


# ==============================================================================
# SECTION 04 — Competitor research helpers (ported verbatim)
# ==============================================================================

def url_domain(value):
    try:
        parsed = urlparse(clean(value, "").strip())
        return parsed.netloc.lower().replace("www.", "")
    except Exception:
        return ""


def url_is_allowed_for_competitor(url, competitor_key):
    domain = url_domain(url)
    allowed_domains = {
        "screwfix": ["screwfix.com"],
        "toolstation": ["toolstation.com"],
        "city_plumbing": ["cityplumbing.co.uk", "cityplumbing.com"],
    }
    for allowed in allowed_domains.get(competitor_key, []):
        if domain == allowed or domain.endswith("." + allowed):
            return True
    return False


def default_competitor_item():
    return {
        "match_found": False, "product_url": "", "competitor_part_number": "",
        "match_confidence": 0.0, "match_reason": "", "safe_to_use_for_price_crawl": False,
    }


def normalise_single_competitor_item(raw_item, competitor_key):
    if not isinstance(raw_item, dict):
        raw_item = {}
    match_found = raw_item.get("match_found") is True
    product_url = clean(raw_item.get("product_url", "")).strip()
    competitor_part_number = clean(raw_item.get("competitor_part_number", "")).strip()
    match_confidence = to_float_safe(raw_item.get("match_confidence", 0), default=0)
    match_reason = clean(raw_item.get("match_reason", "")).strip()
    safe_to_use_from_ai = raw_item.get("safe_to_use_for_price_crawl") is True

    domain_ok = url_is_allowed_for_competitor(product_url, competitor_key)
    if not product_url.startswith("https://"):
        domain_ok = False

    safe_to_use = (match_found and safe_to_use_from_ai and domain_ok
                   and product_url.startswith("https://") and match_confidence >= 0.80)

    if not safe_to_use:
        if not match_reason:
            match_reason = "No safe exact competitor match found."
        if product_url and not domain_ok:
            match_reason = (match_reason + " URL failed Python domain safety check for this competitor.").strip()
        if match_confidence < 0.80 and match_found:
            match_reason = (match_reason + f" Match confidence {match_confidence} is below required 0.80.").strip()

    return {
        "match_found": bool(match_found and domain_ok),
        "product_url": product_url if domain_ok and product_url.startswith("https://") else "",
        "competitor_part_number": competitor_part_number if safe_to_use else "",
        "match_confidence": match_confidence,
        "match_reason": match_reason,
        "safe_to_use_for_price_crawl": safe_to_use,
    }


def normalise_competitor_research(parsed_result):
    raw = parsed_result.get("competitor_research", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "screwfix": normalise_single_competitor_item(raw.get("screwfix", {}), "screwfix"),
        "toolstation": normalise_single_competitor_item(raw.get("toolstation", {}), "toolstation"),
        "city_plumbing": normalise_single_competitor_item(raw.get("city_plumbing", {}), "city_plumbing"),
    }


# ==============================================================================
# SECTION 05 — Cin7 update + OpenAI response helpers (ported verbatim)
# ==============================================================================

def put_cin7_description_update(product, product_id, description_field, html_description,
                                search_term_1, search_term_2, search_term_3, search_term_4,
                                search_terms_mode, barcode_field, barcode_value,
                                should_update_barcode):
    payload = {
        "ID": product_id,
        description_field: html_description,
        "AttributeSet": product.get("AttributeSet"),
        "AdditionalAttribute1": product.get("AdditionalAttribute1", ""),
        "AdditionalAttribute2": product.get("AdditionalAttribute2", ""),
        "AdditionalAttribute3": choose_search_term_value(product.get("AdditionalAttribute3", ""), search_term_1, search_terms_mode),
        "AdditionalAttribute4": choose_search_term_value(product.get("AdditionalAttribute4", ""), search_term_2, search_terms_mode),
        "AdditionalAttribute5": choose_search_term_value(product.get("AdditionalAttribute5", ""), search_term_3, search_terms_mode),
        "AdditionalAttribute6": choose_search_term_value(product.get("AdditionalAttribute6", ""), search_term_4, search_terms_mode),
        "AdditionalAttribute7": product.get("AdditionalAttribute7", ""),
        "AdditionalAttribute8": product.get("AdditionalAttribute8", ""),
        "AdditionalAttribute9": product.get("AdditionalAttribute9", ""),
        "AdditionalAttribute10": product.get("AdditionalAttribute10", ""),
    }
    if should_update_barcode and barcode_field and barcode_value:
        payload[barcode_field] = barcode_value
    payload[COMPLETION_FLAG_FIELD] = COMPLETION_FLAG_VALUE

    rate_limiter.wait()
    response = requests.put(CIN7_PRODUCT_URL, headers=cin7_headers,
                            data=json.dumps(payload), timeout=30)
    try:
        response_body = response.json()
    except Exception:
        response_body = response.text
    return {
        "ok": 200 <= response.status_code < 300,
        "status_code": response.status_code,
        "response": response_body,
        "payload": payload,
    }


def extract_response_text(response_json):
    output_text = response_json.get("output_text", "")
    if output_text:
        return output_text
    collected = []
    for item in response_json.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content_item in item.get("content", []):
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text", "")
            if text:
                collected.append(text)
    return "\n".join(collected).strip()


def safe_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Could not parse JSON from OpenAI response text.")
    return json.loads(match.group(0))


def validate_html_for_catalogue(html):
    html_text = str(html or "")
    html_lower = html_text.lower()
    blocked_patterns = ["<script", "</script", "<iframe", "</iframe", "<form", "</form",
                        "<style", "</style", "javascript:", "onerror=", "onclick=",
                        "onload=", "<img", "<a "]
    issues = [f"Blocked HTML pattern found: {p}" for p in blocked_patterns if p in html_lower]
    allowed_tags = ["div", "h2", "h3", "p", "ul", "li", "strong", "br"]
    found_tags = re.findall(r"</?([a-zA-Z0-9]+)(?:\s[^>]*)?>", html_text)
    for tag in sorted(set(t.lower() for t in found_tags if t.lower() not in allowed_tags)):
        issues.append(f"Unexpected HTML tag found: {tag}")
    return {"html_basic_safe": len(issues) == 0, "html_safety_issues": issues}


def strip_html_to_text(value, max_len=2000):
    text = clean(value, "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text


def escape_catalogue_text(value):
    return html_lib.escape(clean(value, ""), quote=False)


def normalise_description_html(html, clean_product_name, fallback_product_name=""):
    """Applies the MJ Ryder/KPS house style — identical to the Zapier step."""
    html_text = clean(html, "")
    html_text = re.sub(r"<script.*?</script>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"<style.*?</style>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"<iframe.*?</iframe>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"<form.*?</form>", "", html_text, flags=re.IGNORECASE | re.DOTALL)

    h2_match = re.search(r"<h2[^>]*>(.*?)</h2>", html_text, flags=re.IGNORECASE | re.DOTALL)
    product_heading = ""
    if h2_match:
        product_heading = strip_html_to_text(h2_match.group(1), max_len=180)
    if not product_heading:
        product_heading = strip_html_to_text(clean_product_name, max_len=180)
    if not product_heading:
        product_heading = strip_html_to_text(fallback_product_name, max_len=180)
    if not product_heading:
        product_heading = "Product details"

    p_matches = re.findall(r"<p[^>]*>(.*?)</p>", html_text, flags=re.IGNORECASE | re.DOTALL)
    overview = ""
    for paragraph in p_matches:
        paragraph_text = strip_html_to_text(paragraph, max_len=700)
        paragraph_lower = paragraph_text.lower()
        if not paragraph_text:
            continue
        if "refer to the manufacturer" in paragraph_lower and "instructions" in paragraph_lower:
            continue
        overview = paragraph_text
        break
    if not overview:
        full_text = strip_html_to_text(html_text, max_len=700)
        overview = full_text or ("A trade-quality product suitable for plumbing, heating, "
                                 "bathroom or building services use.")

    li_matches = re.findall(r"<li[^>]*>(.*?)</li>", html_text, flags=re.IGNORECASE | re.DOTALL)
    clean_bullets, seen_bullets = [], set()
    for item in li_matches:
        item_text = strip_html_to_text(item, max_len=260)
        item_text = re.sub(r"\s+", " ", item_text).strip()
        if not item_text:
            continue
        item_key = item_text.lower()
        if item_key in seen_bullets:
            continue
        clean_bullets.append(item_text)
        seen_bullets.add(item_key)
    clean_bullets = clean_bullets[:6]
    if not clean_bullets:
        clean_bullets = ["Verified product details should be checked against the manufacturer's latest information."]

    product_heading_html = escape_catalogue_text(product_heading)
    overview_html = escape_catalogue_text(overview)
    bullet_html = "\n".join(f"    <li>{escape_catalogue_text(item)}</li>" for item in clean_bullets)

    return f"""<div>
  <h2><strong>{product_heading_html}</strong></h2>
  <p>{overview_html}</p>
  <h3><strong>Key details</strong></h3>
  <ul>
{bullet_html}
  </ul>
  <p>Refer to the manufacturer's latest instructions before installation.</p>
</div>"""


def is_generic_brand_value(value):
    text = normalise_simple_text_key(value)
    return text in {"", "generic", "kps select", "unbranded", "own brand",
                    "unknown", "not specified", "no brand", "none"}


def generic_category_allowed(category, product_type, product_name):
    combined = normalise_simple_text_key(
        " ".join([clean(category, ""), clean(product_type, ""), clean(product_name, "")]))
    allow_keywords = [
        "threaded brass", "brass fitting", "brass fittings", "brass bush", "reducing bush",
        "bsp bush", "bsp fitting", "bsp female", "bsp male",
        "compression fitting", "compression fittings", "compression adapter",
        "compression adapters", "compression adaptor", "compression adaptors",
        "compression coupler", "compression coupling", "compression female",
        "female coupler", "female coupling", "adapting coupler", "adapting coupling",
        "adapter coupler", "adaptor coupler",
        "copper fitting", "copper fittings", "end feed", "solder ring",
        "waste fitting", "waste fittings", "pipe fitting", "pipe fittings",
        "push fit", "pushfit", "solvent weld", "malleable iron", "galvanised fitting",
        "lever ball valve", "lever ball valves", "ball valve", "ball valves",
        "isolation valve", "isolation valves", "isolating valve", "isolating valves",
        "service valve", "service valves", "gate valve", "gate valves",
        "stop valve", "stop valves", "stopcock", "stopcocks",
        "check valve", "check valves", "single check valve", "single check valves",
        "double check valve", "double check valves",
        "bib tap", "bib taps", "bibcock", "bibcocks",
        "washing machine tap", "washing machine taps", "drain cock", "drain cocks",
        "manual air vent", "manual air vents", "automatic air vent", "automatic air vents",
        "air vent", "air vents", "float valve", "float valves",
        "pump valve", "pump valves", "wallplate adaptor", "wallplate adaptors",
        "wall plate adaptor", "wall plate adaptors",
        "washer", "nut", "bolt", "screw",
    ]
    block_keywords = [
        "boiler", "cylinder", "pump", "valve actuator", "programmer", "thermostat",
        "electrical", "gas", "oil", "chemical", "sealant", "adhesive", "ppe",
        "safety", "fire",
        "pressure reducing valve", "pressure reducing valves",
        "automatic bypass valve", "automatic bypass valves", "bypass valve", "bypass valves",
        "gas valve", "gas valves", "gas cock", "gas cocks",
        "safety valve", "safety valves", "relief valve", "relief valves",
        "temperature relief", "pressure relief", "expansion relief", "unvented",
        "blending valve", "blending valves", "mixing valve", "mixing valves",
        "tmv", "thermostatic mixing valve", "thermostatic mixing valves",
        "zone valve", "zone valves", "motorised valve", "motorised valves",
        "expansion vessel", "flue", "burner",
    ]
    if any(blocked in combined for blocked in block_keywords):
        return False
    return any(allowed in combined for allowed in allow_keywords)


def generic_description_risky_issues(html_description, plain_text_summary):
    combined = normalise_simple_text_key(
        strip_html_to_text(html_description, max_len=5000) + " " + clean(plain_text_summary, ""))
    risky_phrases = [
        "wras", "kiwa", "approved", "certified", "certification", "guarantee", "warranty",
        "pressure rated", "pressure rating", "rated", "working pressure",
        "maximum pressure", "max pressure", "temperature rated", "temperature range",
        "flow rate", "flow rates", "full bore", "pn", "bar", "psi", "gas", "oil",
        "steam", "potable", "drinking water", "lead free", "dzr", "dezincification",
        "bs en", "british standard", "water regulations", "heat resistant", "fire rated",
        "included", "comes with", "compression ends included", "compatible with",
        "suitable for central heating", "suitable for hot water",
        "suitable for cold water", "suitable for gas", "suitable for potable water",
    ]
    return [f"Generic description contains risky/verification-only phrase: {p}"
            for p in risky_phrases if p in combined]


def normalise_web_research(parsed_result, sku, manufacturer, manufacturer_part_number, product_name):
    web_research = parsed_result.get("web_research", {})
    if not isinstance(web_research, dict):
        web_research = {}
    sources_used = parsed_result.get("sources_used", [])
    if not isinstance(sources_used, list):
        sources_used = []

    best_product_page_url = clean(web_research.get("best_product_page_url", "")).strip()
    best_source_type = clean(web_research.get("best_source_type", "")).strip()
    best_source_title = clean(web_research.get("best_source_title", "")).strip()

    if not best_product_page_url:
        for preferred in ["official", "manufacturer"]:
            for source in sources_used:
                if not isinstance(source, dict):
                    continue
                source_type = clean(source.get("source_type", "")).lower()
                url = clean(source.get("url", "")).strip()
                if url.startswith("https://") and preferred in source_type:
                    best_product_page_url = url
                    best_source_type = clean(source.get("source_type", "")).strip()
                    best_source_title = clean(source.get("title", "")).strip()
                    break
            if best_product_page_url:
                break
    if not best_product_page_url:
        for source in sources_used:
            if not isinstance(source, dict):
                continue
            url = clean(source.get("url", "")).strip()
            if url.startswith("https://"):
                best_product_page_url = url
                best_source_type = clean(source.get("source_type", "")).strip()
                best_source_title = clean(source.get("title", "")).strip()
                break

    image_search_hints = web_research.get("image_search_hints", {})
    if not isinstance(image_search_hints, dict):
        image_search_hints = {}
    clean_product_name = clean(parsed_result.get("clean_product_name", "")).strip()

    return {
        "best_product_page_url": best_product_page_url,
        "best_source_type": best_source_type,
        "best_source_title": best_source_title,
        "image_search_hints": {
            "product_name_hint": (clean(image_search_hints.get("product_name_hint", "")).strip()
                                  or clean_product_name or product_name),
            "product_code_hint": (clean(image_search_hints.get("product_code_hint", "")).strip()
                                  or manufacturer_part_number),
            "manufacturer_hint": (clean(image_search_hints.get("manufacturer_hint", "")).strip()
                                  or manufacturer),
            "file_name_base": safe_file_name_base(
                clean(image_search_hints.get("file_name_base", "")).strip()
                or sku or manufacturer_part_number or product_name),
        },
    }


# ==============================================================================
# SECTION 06 — OpenAI research call (prompt + schema IDENTICAL to the Zap)
# ==============================================================================

def competitor_schema_item():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "match_found": {"type": "boolean"},
            "product_url": {"type": "string"},
            "competitor_part_number": {"type": "string"},
            "match_confidence": {"type": "number"},
            "match_reason": {"type": "string"},
            "safe_to_use_for_price_crawl": {"type": "boolean"},
        },
        "required": ["match_found", "product_url", "competitor_part_number",
                     "match_confidence", "match_reason", "safe_to_use_for_price_crawl"],
    }


def build_research_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "safe_to_use_for_draft": {"type": "boolean"},
            "safe_to_auto_update_cin7": {"type": "boolean"},
            "confidence": {"type": "number"},
            "manufacturer": {"type": "string"},
            "cin7_sku": {"type": "string"},
            "manufacturer_part_number": {"type": "string"},
            "product_name": {"type": "string"},
            "clean_product_name": {"type": "string"},
            "product_type": {"type": "string"},
            "sku_match_status": {
                "type": "string",
                "description": ("One of: exact_official_match, exact_trusted_supplier_match, "
                                "manufacturer_part_number_match, model_match_only, conflicting_sku, not_verified"),
            },
            "source_quality": {
                "type": "string",
                "description": "One of: official_manufacturer, trusted_supplier, mixed_sources, weak_sources, not_verified",
            },
            "verified_facts": {"type": "array", "items": {"type": "string"}},
            "unverified_or_missing_facts": {"type": "array", "items": {"type": "string"}},
            "html_description": {"type": "string"},
            "plain_text_summary": {"type": "string"},
            "meta_title": {"type": "string"},
            "meta_description": {"type": "string"},
            "search_term_1": {"type": "string"},
            "search_term_2": {"type": "string"},
            "search_term_3": {"type": "string"},
            "search_term_4": {"type": "string"},
            "barcode_research": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "barcode_found": {"type": "boolean"},
                    "barcode_value": {"type": "string"},
                    "barcode_type": {"type": "string"},
                    "barcode_source_url": {"type": "string"},
                    "barcode_source_type": {"type": "string"},
                    "confidence": {"type": "number"},
                    "safe_to_update_cin7_barcode": {"type": "boolean"},
                    "barcode_notes": {"type": "string"},
                },
                "required": ["barcode_found", "barcode_value", "barcode_type",
                             "barcode_source_url", "barcode_source_type", "confidence",
                             "safe_to_update_cin7_barcode", "barcode_notes"],
            },
            "web_research": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "best_product_page_url": {"type": "string"},
                    "best_source_type": {"type": "string"},
                    "best_source_title": {"type": "string"},
                    "manufacturer_domain": {"type": "string"},
                    "trusted_source_urls": {"type": "array", "items": {"type": "string"}},
                    "image_search_hints": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "product_name_hint": {"type": "string"},
                            "product_code_hint": {"type": "string"},
                            "manufacturer_hint": {"type": "string"},
                            "file_name_base": {"type": "string"},
                        },
                        "required": ["product_name_hint", "product_code_hint",
                                     "manufacturer_hint", "file_name_base"],
                    },
                },
                "required": ["best_product_page_url", "best_source_type", "best_source_title",
                             "manufacturer_domain", "trusted_source_urls", "image_search_hints"],
            },
            "competitor_research": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "screwfix": competitor_schema_item(),
                    "toolstation": competitor_schema_item(),
                    "city_plumbing": competitor_schema_item(),
                },
                "required": ["screwfix", "toolstation", "city_plumbing"],
            },
            "sources_used": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "source_type": {"type": "string"},
                        "facts_supported": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "url", "source_type", "facts_supported"],
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
            "recommended_next_action": {"type": "string"},
        },
        "required": [
            "safe_to_use_for_draft", "safe_to_auto_update_cin7", "confidence",
            "manufacturer", "cin7_sku", "manufacturer_part_number", "product_name",
            "clean_product_name", "product_type", "sku_match_status", "source_quality",
            "verified_facts", "unverified_or_missing_facts", "html_description",
            "plain_text_summary", "meta_title", "meta_description",
            "search_term_1", "search_term_2", "search_term_3", "search_term_4",
            "barcode_research", "web_research", "competitor_research", "sources_used",
            "warnings", "recommended_next_action",
        ],
    }


SYSTEM_PROMPT = """
You are a careful UK plumbing, heating, bathroom, and trade catalogue content researcher.

Your job:
- Use web search to research the product.
- Prioritise official manufacturer pages and official PDFs.
- Trusted UK plumbing/heating/bathroom merchants are acceptable secondary sources.
- Do not use marketplaces, forums, random scraped catalogues, or unsourced claims unless clearly labelled weak.
- Do not invent technical specifications.
- Only include facts supported by sources found during web search.

Strict verification rules:
- Only mention included accessories, stands, brackets, batteries, dimensions, warranty, guarantee, colours, finishes, outputs, flow rates, ratings, approvals, compatibility, package contents, or installation requirements if they are explicitly verified in official manufacturer sources or strong trusted supplier sources.
- Do not include warranty or guarantee claims in the HTML description unless the full warranty terms for this exact product have been verified.
- If a stand, bracket, receiver, cable, fitting, battery, or accessory is mentioned, be clear whether it is included in the pack, required separately, or merely compatible/available separately.
- If dimensions are mentioned, state what they apply to, for example thermostat dimensions, receiver dimensions, product dimensions, or pack dimensions.

Important SKU logic:
- The Cin7 SKU may include an internal supplier prefix in the format PREFIX-MANUFACTURERPARTNO.
- Use manufacturer_part_number as the primary code to verify against official manufacturer or trusted supplier sources.
- Do not mark the result as conflicting_sku simply because the Cin7 SKU has a prefix.
- safe_to_auto_update_cin7 should only be true when the manufacturer_part_number or exact Cin7 SKU is confidently matched from official manufacturer or strong trusted supplier sources.

Source-quality classification rules:
- Before classifying source_quality as mixed_sources or weak_sources, actively search the manufacturer's own website for the manufacturer_part_number and product name.
- If an official manufacturer product page confirms the exact manufacturer_part_number, classify source_quality as official_manufacturer even if trusted supplier pages were also used for supporting facts.
- If an official manufacturer PDF, technical sheet, datasheet, or installation guide confirms the exact manufacturer_part_number, classify source_quality as official_manufacturer_pdf even if trusted supplier pages were also used for supporting facts.
- Use mixed_sources only when no single official manufacturer or trusted supplier source is sufficient on its own, but multiple credible sources together support the product match.
- Use weak_sources or not_verified when the exact product cannot be verified from official manufacturer or strong trusted supplier sources.

Generic / KPS Select commodity product rules:
- Some Cin7 records are intentionally Generic, KPS Select, or unbranded commodity items where the supplier may vary, for example brass bushes, compression fittings, copper fittings, waste fittings, washers, nuts, bolts, screws, simple pipe fittings, and basic everyday trade valves/taps.
- For genuinely generic/KPS Select/unbranded commodity items, it is acceptable to draft a cautious generic description based on the product name, visible size, material, category and common fitting type, even when no exact brand/manufacturer part number can be verified.
- For generic/KPS Select commodity items, do not claim an exact brand, barcode, official manufacturer, approval, WRAS certification, KIWA certification, DZR brass, pressure rating, flow rate, temperature range, warranty, guarantee, standard, compatibility, potable-water suitability, gas suitability, oil suitability, steam suitability, included accessory, or suitability for central heating/hot water/cold water unless directly verified.
- For generic/KPS Select commodity items, safe_to_auto_update_cin7 may remain false if exact part-number verification is not possible; the local Python code has a separate generic-description lane and will decide whether the cautious description is safe enough to write.
- For generic/KPS Select commodity items, use sku_match_status model_match_only when matching is by size/type rather than exact brand/model.
- For generic/KPS Select commodity items, source_quality may be mixed_sources where several credible merchant pages support the generic product type and wording, but no exact manufacturer source exists.

Description rules:
- Return clean HTML suitable for a Cin7/web product description field.
- The HTML should be useful for a trade/customer web catalogue, not just a technical manual.
- Keep the HTML concise but useful.
- Do not include source URLs in the HTML.
- Do not include warnings in the HTML.
- Do not mention source names, supplier names, retailer names, merchant names, or competitor names in the HTML description.
- Do not write phrases such as "listed by", "shown by", "according to", "as stated by", "as shown on", "as listed on", "supplier page", "merchant page", "retailer page", or "search results" in the HTML description.
- Source attribution belongs only in sources_used, warnings, match_reason, or internal notes. It must never appear in the customer-facing HTML description.
- If a fact is only available from a merchant or retailer source, either phrase it as a clean product fact without naming the source, or leave it out if it is not strong enough for customer-facing copy.
- Do not include merchant-specific wording such as City Plumbing, Screwfix, Toolstation, supplier part page, or retailer listing in the HTML description unless that company is the actual manufacturer of the product.
- When an official manufacturer product page is found, use the manufacturer page as the primary source for the overview and key details.
- For exact branded products, prefer manufacturer-confirmed functional features over sparse generic copy.
- Where the official manufacturer page provides several useful features, write 1 to 2 polished overview sentences and 5 to 6 distinct customer-useful key-detail bullets.
- Do not over-compress several unrelated features into one long bullet. Split features into readable separate bullets where possible.
- Do not omit important manufacturer-confirmed features such as heat settings, thermostat, indicator lights, carry handles, cut-out protection, intended use, material, finish, dimensions, outputs, compatibility, or pack contents when they are clearly listed on the official product page.
- Avoid vague compliance wording such as "certification to European Standards" or "approved to standards". Only include specific approvals, such as BEAB approved, WRAS approved, KIWA approved, or BS EN references, when clearly verified by an official manufacturer source or official manufacturer PDF.
- Keep the wording concise, but do not make the description so sparse that useful manufacturer-confirmed selling points are lost.

HTML format must always follow this structure:
<div>
  <h2><strong>Clean product name</strong></h2>
  <p>One concise plain-English overview paragraph.</p>
  <h3><strong>Key details</strong></h3>
  <ul>
    <li>Verified fact</li>
    <li>Verified fact</li>
  </ul>
  <p>Refer to the manufacturer's latest instructions before installation.</p>
</div>

Allowed HTML tags:
- div
- h2
- h3
- p
- ul
- li
- strong
- br

Strong tag rules:
- Use <strong> only inside the <h2> product title and the <h3> Key details heading.
- Do not use <strong> inside paragraphs or bullet points for product codes, sizes, finishes, brands, dimensions, or part numbers.

Do not include:
- scripts
- styles
- images
- links
- tables
- forms
- tracking code
- inline CSS
- unsupported HTML tags

Search term rules:
- Return exactly 4 search terms.
- Use a mix of official catalogue wording and common UK trade/plumber search language.
- Prefer terms someone would realistically type into a trade counter or web catalogue search box.
- Include at least 1 trade/plumber-style term where appropriate.
- No commas.
- No HTML.
- No full sentences.
- No quotation marks.
- Maximum 35 characters per term.
- Use UK terminology.

Barcode research rules:
- Look for a genuine product barcode, EAN, UPC, or GTIN for the exact product/manufacturer part number.
- Prefer official manufacturer product pages, official PDFs, technical datasheets, or trusted supplier pages.
- Do not guess a barcode.
- Do not infer a barcode from a similar product.
- Do not use a barcode unless it is clearly tied to the exact manufacturer_part_number or exact product model.
- barcode_value should contain digits only where possible.
- barcode_type should be EAN-8, UPC-A, EAN-13, GTIN-14, or unknown.
- safe_to_update_cin7_barcode should only be true if the barcode is clearly verified for this exact product.
- If no verified barcode is found, set barcode_found=false, barcode_value="", confidence=0, safe_to_update_cin7_barcode=false.

Web research object rules:
- Populate web_research for the next image-upload code step.
- best_product_page_url should be the best product-specific page URL for image discovery.
- Prefer official manufacturer product pages over PDFs.
- best_source_type should be official_manufacturer, official_manufacturer_pdf, trusted_supplier, or fallback_source.
- manufacturer_domain should be the main manufacturer domain if known.
- trusted_source_urls should include up to 5 useful HTTPS source URLs used during research.
- image_search_hints.product_name_hint should be the clean product name suitable for image scoring.
- image_search_hints.product_code_hint should be the manufacturer part number, not the internal Cin7 prefix SKU.
- image_search_hints.manufacturer_hint should be the manufacturer/brand name.
- image_search_hints.file_name_base should be a sensible filename base for uploaded images.

Competitor research rules:
- Research competitor product pages only for these three UK competitors:
  1. Screwfix
  2. Toolstation
  3. City Plumbing
- Do not return competitor matches from any other retailer.
- For Screwfix, only return URLs from screwfix.com.
- For Toolstation, only return URLs from toolstation.com.
- For City Plumbing, only return URLs from cityplumbing.co.uk or cityplumbing.com.
- Search using the manufacturer_part_number first.
- Also use the clean product name, manufacturer, and barcode if available.
- The competitor product must be the same product, not just a similar alternative.
- Prefer a manufacturer part number, MPN, product code, catalogue number, or retailer product code visible on the competitor page.
- competitor_part_number should be the competitor's own visible product code / product number if available.
- If only the manufacturer part number is visible, use that and explain it in match_reason.
- If you cannot verify the exact product, set match_found=false, product_url="", competitor_part_number="", match_confidence=0, safe_to_use_for_price_crawl=false.
- safe_to_use_for_price_crawl should only be true when the exact product is confidently matched.
- Do not guess competitor URLs.
- Do not create URLs from search-result snippets.
- Do not use sponsored, marketplace, image-only, category, search results, or listing pages as product_url.
- Return the direct product page URL where possible.

Return only valid JSON matching the supplied schema.
"""


def research_product_with_openai(manufacturer, cin7_sku, manufacturer_part_number,
                                 product_name, existing_descriptions):
    user_prompt = {
        "task": (
            "Research this Cin7 product and create a proposed HTML long description, "
            "4 product search terms, barcode research, web_research object for later image discovery, "
            "and competitor product URLs/part numbers for Screwfix, Toolstation, and City Plumbing."
        ),
        "product": {
            "manufacturer": manufacturer,
            "cin7_sku": cin7_sku,
            "manufacturer_part_number": manufacturer_part_number,
            "product_name": product_name,
            "existing_descriptions": existing_descriptions,
        },
        "competitor_targets": {
            "screwfix": "screwfix.com",
            "toolstation": "toolstation.com",
            "city_plumbing": "cityplumbing.co.uk",
        },
        "important_instruction": (
            "Use manufacturer_part_number as the primary manufacturer/source code. "
            "Do not treat the Cin7 prefix as a mismatch if the manufacturer_part_number is verified. "
            "Do not copy existing description text blindly; improve it only using verified sources. "
            "Return barcode_research only when the barcode is clearly tied to the exact product. "
            "If an official manufacturer page or official manufacturer PDF confirms the exact part number, "
            "classify source_quality as official_manufacturer or official_manufacturer_pdf rather than mixed_sources. "
            "For Generic, KPS Select, or unbranded commodity fittings/basic trade valves, keep the description cautious and generic: "
            "only use product name, size, material, category and fitting type unless stronger evidence is found. "
            "Return competitor_research only for exact product matches from Screwfix, Toolstation, and City Plumbing. "
            "Do not include source names, retailer names, or phrases like listed by, shown by, or according to in the customer-facing HTML description. "
            "When an official manufacturer page is found, use its confirmed feature list to write a useful but concise product description "
            "rather than sparse generic copy. Keep key details as distinct readable bullets rather than over-compressing unrelated features. "
            "Avoid vague compliance wording; only use specific approval names when clearly verified."
        ),
    }

    payload = {
        "model": CFG["ENRICH_MODEL"],
        "tools": [{"type": "web_search", "search_context_size": "medium"}],
        "tool_choice": "required",
        "max_output_tokens": 4500,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {"role": "user", "content": json.dumps(user_prompt)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "cin7_product_description_research",
                "schema": build_research_schema(),
                "strict": True,
            }
        },
    }

    # Batch runs can afford a longer timeout than the Zap's 25s, and one retry
    # on transient failures saves losing a product to a network blip.
    last_error = None
    for attempt in (1, 2):
        try:
            response = requests.post(OPENAI_RESPONSES_URL, headers=openai_headers,
                                     json=payload, timeout=180)
            break
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt == 2:
                raise ValueError(f"OpenAI request failed after retry: {e}")
            time.sleep(10)

    status_code = response.status_code
    try:
        response_json = response.json()
    except Exception:
        response_json = {"raw_text": response.text}

    if status_code < 200 or status_code >= 300:
        raise ValueError(f"OpenAI API request failed: {status_code} - {json.dumps(response_json)[:3000]}")

    response_text = extract_response_text(response_json)
    if not response_text:
        raise ValueError(f"OpenAI response did not contain output text. Raw: {json.dumps(response_json)[:3000]}")

    return {"status_code": status_code, "parsed_result": safe_json_loads(response_text)}


# ==============================================================================
# SECTION 07 — Enrich one product (the Zap's SECTION 06, as a function)
# ==============================================================================

LOG_FIELDS = [
    "SKU", "Name", "Brand", "Category", "Action", "Lane", "DryRun",
    "Confidence", "SkuMatchStatus", "SourceQuality",
    "DescriptionLength", "SearchTerm1", "SearchTerm2", "SearchTerm3", "SearchTerm4",
    "BarcodeFound", "BarcodeValue", "BarcodeSent",
    "HasExistingImages", "BestProductPageUrl",
    "ScrewfixUrl", "ScrewfixPartNo", "ScrewfixSafe",
    "ToolstationUrl", "ToolstationPartNo", "ToolstationSafe",
    "CityPlumbingUrl", "CityPlumbingPartNo", "CityPlumbingSafe",
    "BlockReasons", "PutStatus", "Success", "Error",
]


def _log_row(sku, name="", brand="", category="", action="", **kw):
    row = {f: "" for f in LOG_FIELDS}
    row.update({"SKU": sku, "Name": name, "Brand": brand, "Category": category,
                "Action": action, "DryRun": CFG["DRY_RUN"]})
    row.update(kw)
    return row


def enrich_one_product(sku):
    """Full enrichment flow for one SKU. Returns a log-row dict."""
    dry_run = CFG["DRY_RUN"]
    description_field = CFG["ENRICH_DESCRIPTION_FIELD"]
    only_update_if_blank = CFG["ENRICH_ONLY_IF_BLANK"]
    search_terms_mode = CFG["ENRICH_SEARCH_TERMS_MODE"]
    min_confidence = CFG["ENRICH_MIN_CONFIDENCE"]
    only_update_barcode_if_blank = CFG["ENRICH_BARCODE_ONLY_IF_BLANK"]
    min_barcode_confidence = CFG["ENRICH_MIN_BARCODE_CONFIDENCE"]
    manufacturer_field = CFG["ENRICH_MANUFACTURER_FIELD"]

    product = get_cin7_product_by_sku(sku)

    product_id = clean(product.get("ID", ""))
    product_name = clean(product.get("Name", "")).strip()
    category = clean(product.get("Category", "")).strip()
    brand = clean(product.get("Brand", "")).strip()
    if not product_id or not product_name:
        raise ValueError("Cin7 product response missing ID or Name")

    # --- Gate 0: completion flag / blank-description targeting -----------------
    flag_raw = clean(product.get(COMPLETION_FLAG_FIELD, "")).strip().lower()
    if flag_raw == COMPLETION_FLAG_VALUE:
        return _log_row(sku, product_name, brand, category, "skipped_flag_set",
                        Success=True)

    existing_target_description = clean(product.get(description_field, ""))
    existing_target_description_is_blank = blankish(existing_target_description)
    if only_update_if_blank and not existing_target_description_is_blank:
        return _log_row(sku, product_name, brand, category, "skipped_description_present",
                        Success=True)

    existing_image_summary = summarise_existing_image_attachments(product)
    has_existing_image_attachments = existing_image_summary["has_existing_image_attachments"]

    manufacturer, _src = get_product_manufacturer(product, manufacturer_field)
    if not manufacturer:
        manufacturer = product_name.split(" ")[0].strip()
    manufacturer_part_number = derive_manufacturer_part_number_from_sku(sku)
    existing_descriptions = get_first_existing_description(product)

    # --- OpenAI research --------------------------------------------------------
    research = research_product_with_openai(
        manufacturer=manufacturer, cin7_sku=sku,
        manufacturer_part_number=manufacturer_part_number,
        product_name=product_name, existing_descriptions=existing_descriptions)
    parsed_result = research.get("parsed_result", {})

    raw_html_description = clean(parsed_result.get("html_description", ""))
    html_description = normalise_description_html(
        html=raw_html_description,
        clean_product_name=parsed_result.get("clean_product_name", ""),
        fallback_product_name=product_name)
    html_safety = validate_html_for_catalogue(html_description)

    confidence = to_float_safe(parsed_result.get("confidence"), default=0)
    source_quality = clean(parsed_result.get("source_quality", ""))
    sku_match_status = clean(parsed_result.get("sku_match_status", ""))
    safe_to_auto_update_cin7_from_ai = parsed_result.get("safe_to_auto_update_cin7") is True

    proposed = [sanitise_search_term(parsed_result.get(f"search_term_{i}", "")) for i in (1, 2, 3, 4)]
    finals = [
        choose_search_term_value(product.get("AdditionalAttribute3", ""), proposed[0], search_terms_mode),
        choose_search_term_value(product.get("AdditionalAttribute4", ""), proposed[1], search_terms_mode),
        choose_search_term_value(product.get("AdditionalAttribute5", ""), proposed[2], search_terms_mode),
        choose_search_term_value(product.get("AdditionalAttribute6", ""), proposed[3], search_terms_mode),
    ]

    web_research = normalise_web_research(parsed_result, sku, manufacturer,
                                          manufacturer_part_number, product_name)
    competitor_research = normalise_competitor_research(parsed_result)
    barcode_decision = decide_barcode_update(
        product=product,
        parsed_barcode_research=parsed_result.get("barcode_research", {}),
        barcode_field_name="Barcode",
        only_update_if_blank=only_update_barcode_if_blank,
        min_barcode_confidence=min_barcode_confidence)

    # --- Local safety gates (identical two-lane logic to the Zap) --------------
    allowed_sku_match_statuses = ["exact_official_match", "exact_trusted_supplier_match",
                                  "manufacturer_part_number_match"]
    allowed_source_qualities = ["official_manufacturer", "official_manufacturer_pdf",
                                "trusted_supplier"]
    mixed_sources_min_confidence = 0.95
    mixed_sources_high_confidence_allowed = (
        source_quality == "mixed_sources"
        and confidence >= mixed_sources_min_confidence
        and sku_match_status in allowed_sku_match_statuses
        and safe_to_auto_update_cin7_from_ai is True)
    source_quality_allowed = (source_quality in allowed_source_qualities
                              or mixed_sources_high_confidence_allowed)

    exact_search_terms_available = all(f != "" for f in finals)
    useful_search_terms_count = count_useful_search_terms(*finals)
    html_description_available = bool(html_description.strip())
    html_basic_safe = html_safety.get("html_basic_safe") is True
    blank_field_rule_passed = existing_target_description_is_blank or not only_update_if_blank

    exact_product_description_allowed = (
        safe_to_auto_update_cin7_from_ai is True
        and confidence >= min_confidence
        and sku_match_status in allowed_sku_match_statuses
        and source_quality_allowed
        and html_description_available
        and html_basic_safe
        and exact_search_terms_available
        and blank_field_rule_passed)

    exact_block_reasons = []
    if safe_to_auto_update_cin7_from_ai is not True:
        exact_block_reasons.append("OpenAI did not mark safe_to_auto_update_cin7=true.")
    if confidence < min_confidence:
        exact_block_reasons.append(f"Confidence {confidence} below min_confidence {min_confidence}.")
    if sku_match_status not in allowed_sku_match_statuses:
        exact_block_reasons.append(f"SKU match status '{sku_match_status}' not allowed.")
    if not source_quality_allowed:
        exact_block_reasons.append(f"Source quality '{source_quality}' not allowed.")
    if not exact_search_terms_available:
        exact_block_reasons.append("One or more final search terms are blank.")

    generic_min_confidence = 0.50
    is_generic_catalogue_item = (is_generic_brand_value(brand)
                                 or is_generic_brand_value(manufacturer))
    generic_category_is_allowed = generic_category_allowed(
        category=category, product_type=parsed_result.get("product_type", ""),
        product_name=product_name)
    generic_risky = generic_description_risky_issues(
        html_description=html_description,
        plain_text_summary=parsed_result.get("plain_text_summary", ""))

    generic_description_allowed = (
        is_generic_catalogue_item
        and generic_category_is_allowed
        and confidence >= generic_min_confidence
        and sku_match_status in ["model_match_only", "manufacturer_part_number_match", "not_verified"]
        and source_quality in ["mixed_sources", "trusted_supplier"]
        and html_description_available
        and html_basic_safe
        and len(generic_risky) == 0
        and useful_search_terms_count >= 2
        and blank_field_rule_passed)

    generic_block_reasons = []
    if not is_generic_catalogue_item:
        generic_block_reasons.append("Not a Generic/KPS Select/unbranded item.")
    if not generic_category_is_allowed:
        generic_block_reasons.append("Category/type/name not in the safe generic list.")
    if generic_risky:
        generic_block_reasons.extend(generic_risky)
    if useful_search_terms_count < 2:
        generic_block_reasons.append(f"Only {useful_search_terms_count} useful search terms (need 2).")

    local_safe = exact_product_description_allowed or generic_description_allowed
    lane = ("exact_product" if exact_product_description_allowed
            else "generic_commodity_description" if generic_description_allowed
            else "blocked")

    barcode_would_update = barcode_decision.get("would_update_cin7_barcode") is True
    barcode_send = barcode_would_update and exact_product_description_allowed

    block_reasons = []
    if not html_description_available:
        block_reasons.append("No usable HTML description returned.")
    if not html_basic_safe:
        block_reasons.append("HTML failed safety check: "
                             + " | ".join(html_safety.get("html_safety_issues", [])))
    if not blank_field_rule_passed:
        block_reasons.append("Existing description not blank and ENRICH_ONLY_IF_BLANK=True.")
    if not local_safe:
        block_reasons.append("Exact lane: " + (" | ".join(exact_block_reasons) or "passed checks not met"))
        block_reasons.append("Generic lane: " + (" | ".join(generic_block_reasons) or "passed checks not met"))

    scr = competitor_research["screwfix"]
    tls = competitor_research["toolstation"]
    cpl = competitor_research["city_plumbing"]
    common = dict(
        Lane=lane, Confidence=confidence, SkuMatchStatus=sku_match_status,
        SourceQuality=source_quality, DescriptionLength=len(html_description),
        SearchTerm1=finals[0], SearchTerm2=finals[1], SearchTerm3=finals[2], SearchTerm4=finals[3],
        BarcodeFound=barcode_decision.get("barcode_found", False),
        BarcodeValue=barcode_decision.get("barcode_value", ""),
        HasExistingImages=has_existing_image_attachments,
        BestProductPageUrl=web_research.get("best_product_page_url", ""),
        ScrewfixUrl=scr["product_url"], ScrewfixPartNo=scr["competitor_part_number"],
        ScrewfixSafe=scr["safe_to_use_for_price_crawl"],
        ToolstationUrl=tls["product_url"], ToolstationPartNo=tls["competitor_part_number"],
        ToolstationSafe=tls["safe_to_use_for_price_crawl"],
        CityPlumbingUrl=cpl["product_url"], CityPlumbingPartNo=cpl["competitor_part_number"],
        CityPlumbingSafe=cpl["safe_to_use_for_price_crawl"],
        BlockReasons=clean_text(" | ".join(block_reasons), max_len=1000),
    )

    if not local_safe:
        print(f"  BLOCKED {sku}: lane gates not passed (see log).")
        return _log_row(sku, product_name, brand, category, "blocked", Success=False, **common)

    if dry_run:
        print(f"  [DRY RUN] {sku}: would write description ({len(html_description)} chars, "
              f"lane {lane})" + (f" + barcode {barcode_decision.get('barcode_value')}" if barcode_send else ""))
        return _log_row(sku, product_name, brand, category, "would_enrich", Success=True, **common)

    put_result = put_cin7_description_update(
        product=product, product_id=product_id,
        description_field=description_field, html_description=html_description,
        search_term_1=proposed[0], search_term_2=proposed[1],
        search_term_3=proposed[2], search_term_4=proposed[3],
        search_terms_mode=search_terms_mode,
        barcode_field="Barcode",
        barcode_value=barcode_decision.get("barcode_value", ""),
        should_update_barcode=barcode_send)

    if not put_result.get("ok"):
        raise ValueError(f"Cin7 PUT failed: {put_result.get('status_code')} - "
                         f"{str(put_result.get('response'))[:1000]}")

    print(f"  [OK] {sku}: description written (lane {lane})"
          + (f" + barcode {barcode_decision.get('barcode_value')}" if barcode_send else "")
          + f" | flag {COMPLETION_FLAG_FIELD}=true")
    return _log_row(sku, product_name, brand, category, "enriched", Success=True,
                    BarcodeSent=(barcode_decision.get("barcode_value", "") if barcode_send else ""),
                    PutStatus=put_result.get("status_code", ""), **common)


# ==============================================================================
# SECTION 08 — Target selection (caches first, API scan as fallback)
# ==============================================================================

def _cache_fresh(path, max_age_hours):
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(200)
        m = re.search(r'"generated"\s*:\s*"([^"]+)"', head)
        if not m:
            return False
        built = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        return (datetime.now() - built).total_seconds() / 3600.0 <= max_age_hours
    except Exception:
        return False


def select_candidates():
    """Return a list of candidate SKUs (strings) matching the Config scope.

    Prefers the export cache (full records) because it lets us pre-filter on
    the completion flag and blank description locally, skipping products
    without spending a Cin7 GET on them. Falls back to the slim index (scope
    filter only — the per-product GET then gates flag/description), and to a
    fresh lightweight scan when neither cache is fresh.
    """
    brand = CFG["BRAND_FILTER"].strip().lower()
    cat   = CFG["CATEGORY_FILTER"].strip().lower()
    xbb   = CFG["EXCLUDE_BATHROOM_BRANDS"]
    max_age = min(CFG["CATALOGUE_MAX_AGE_HOURS"], 24)

    def in_scope(p_brand, p_cat):
        b, c = p_brand.strip().lower(), p_cat.strip().lower()
        if xbb and "bathroom brands" in (b, c):
            return False
        if brand and b != brand:
            return False
        if cat and c != cat:
            return False
        return True

    # 1) Export cache: full records -> pre-filter flag + blank description.
    if os.path.exists(EXPORT_CACHE_PATH) and _cache_fresh(EXPORT_CACHE_PATH, max_age):
        try:
            with open(EXPORT_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            products = data.get("products", [])
            out = []
            for p in products:
                if not in_scope(clean(p.get("Brand", "")), clean(p.get("Category", ""))):
                    continue
                if clean(p.get(COMPLETION_FLAG_FIELD, "")).strip().lower() == COMPLETION_FLAG_VALUE:
                    continue
                if CFG["ENRICH_ONLY_IF_BLANK"] and not blankish(p.get(CFG["ENRICH_DESCRIPTION_FIELD"], "")):
                    continue
                s = clean(p.get("SKU", "")).strip()
                if s:
                    out.append(s)
            print(f"  Targets: {len(out)} candidate(s) pre-filtered from export_cache.json "
                  f"(built {data.get('generated', '?')}).")
            return out
        except Exception as e:
            print(f"  export_cache.json unreadable ({e}) — falling back to the slim index.")

    # 2) Slim index: scope filter only (per-product GET gates the rest).
    products = None
    if os.path.exists(CATALOGUE_CACHE_PATH) and _cache_fresh(CATALOGUE_CACHE_PATH, max_age):
        try:
            with open(CATALOGUE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            products = data.get("products", [])
            print(f"  Targets: scope-filtering catalogue_index.json (built {data.get('generated', '?')}); "
                  f"flag/description checked per product at GET time.")
        except Exception as e:
            print(f"  catalogue_index.json unreadable ({e}) — scanning fresh.")

    # 3) Fresh lightweight scan.
    if products is None:
        print("  Targets: no fresh cache — lightweight catalogue scan (~1.5 min)...")
        products, page, total = [], 1, None
        while True:
            rate_limiter.wait()
            r = requests.get(CIN7_PRODUCT_URL, headers=cin7_headers,
                             params={"Page": page, "Limit": 1000}, timeout=60)
            if not (200 <= r.status_code < 300):
                sys.exit(f"Cin7 product list failed (page {page}): {r.status_code} - {r.text[:300]}")
            body = r.json()
            batch = body.get("Products", []) if isinstance(body, dict) else (body or [])
            if isinstance(body, dict):
                total = body.get("Total", total)
            if not batch:
                break
            products.extend(batch)
            print(f"    scanned page {page} ({len(products)} products)...", end="\r")
            if total is not None and page * 1000 >= int(total):
                break
            if len(batch) < 1000:
                break
            page += 1
        print(" " * 60, end="\r")

    out = []
    for p in products:
        if in_scope(clean(p.get("Brand", "")), clean(p.get("Category", ""))):
            s = clean(p.get("SKU", "")).strip()
            if s:
                out.append(s)
    print(f"  Targets: {len(out)} in scope (flag/description checked per product at GET time).")
    return out


# ==============================================================================
# SECTION 09 — Main
# ==============================================================================

def main():
    init_runtime()
    global rate_limiter
    rate_limiter = RateLimiter(CFG["RATE_LIMIT_PER_MIN"])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = os.path.join(SCRIPT_DIR, "Logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"enrich_log_{ts}.csv")

    print("=" * 66)
    print("RHS Group Ltd — Cin7 Product Description Enricher (batch)")
    print(f"Mode:        {'DRY RUN (no changes will be made)' if CFG['DRY_RUN'] else '*** LIVE — Cin7 WILL be updated ***'}")
    print(f"Scope:       Brand='{CFG['BRAND_FILTER'] or '(any)'}'  Category='{CFG['CATEGORY_FILTER'] or '(any)'}'")
    print(f"Cap:         {CFG['ENRICH_MAX_PRODUCTS']} product(s) this run")
    print(f"Model:       {CFG['ENRICH_MODEL']}")
    print(f"Desc field:  {CFG['ENRICH_DESCRIPTION_FIELD']} (only if blank: {CFG['ENRICH_ONLY_IF_BLANK']})")
    print(f"Log file:    {log_path}")
    print("=" * 66)

    if not CFG["BRAND_FILTER"] and not CFG["CATEGORY_FILTER"]:
        print("\nERROR: both BRAND_FILTER and CATEGORY_FILTER are blank.")
        print("Refusing to enrich the entire catalogue. Set at least one in Config.yaml.")
        return

    print("\nSelecting targets...")
    candidates = select_candidates()
    if not candidates:
        print("\nNothing to enrich in this scope. Done.")
        return

    cap = max(1, CFG["ENRICH_MAX_PRODUCTS"])
    est = min(cap, len(candidates))
    print(f"\n  This run will enrich up to {est} product(s) "
          f"(~{est * 0.5:.0f}-{est * 1.5:.0f} min; one OpenAI web-search call each — billable).")

    if not CFG["DRY_RUN"]:
        print("\n  LIVE RUN checks:")
        print("  - Is the Zapier enrichment Zap OFF (or excluding these products)?")
        print("  - Is NO live cin7_price_updater.py run in progress?")
        print("  Type CONFIRM to proceed, or press Enter to cancel:")
        try:
            if input("  > ").strip().upper() != "CONFIRM":
                print("  Cancelled. No changes made.")
                return
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled. No changes made.")
            return
    print()

    results = []
    enriched = 0
    try:
        for i, sku in enumerate(candidates, 1):
            if enriched >= cap:
                print(f"\nCap of {cap} reached — stopping (resume any time; the "
                      f"{COMPLETION_FLAG_FIELD} flag skips completed products).")
                break
            print(f"[{i}/{len(candidates)}] {sku}")
            try:
                row = enrich_one_product(sku)
            except Exception as e:
                print(f"  ERROR {sku}: {clean_text(str(e), 300)}")
                row = _log_row(sku, action="error", Success=False,
                               Error=clean_text(str(e), 1000))
            results.append(row)
            if row["Action"] in ("enriched", "would_enrich", "blocked", "error"):
                enriched += 1     # anything that consumed an OpenAI call counts
    except KeyboardInterrupt:
        print("\n\nCtrl+C — stopping early; writing the log for completed products.")

    with open(log_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(results)

    counts = {}
    for r in results:
        counts[r["Action"]] = counts.get(r["Action"], 0) + 1
    print("\n" + "=" * 66)
    print("Run complete: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print(f"Log saved to: {log_path}")
    if CFG["DRY_RUN"]:
        print("\nThis was a DRY RUN. Set DRY_RUN: False in Config.yaml to apply.")
    print("=" * 66)


if __name__ == "__main__":
    main()
