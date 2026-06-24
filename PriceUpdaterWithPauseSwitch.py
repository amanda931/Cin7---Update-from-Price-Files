# ==============================================================================
# RHS Group Ltd — Supplier Price Update Script
# ==============================================================================
# Purpose:
#   Reads a supplier price CSV file and updates purchase costs, all price tiers,
#   simPRO catalogue prices, and Shopify variant prices for each SKU in the file.
#
# CSV format required (one header row, then data):
#   SKU, Name, Cost
#
#   Optional extra columns (Phase 1): add any column listed in ATTRIBUTE_COLUMN_MAP
#   below and its value is written to the matching Cin7 field. "Barcode" is where
#   Cin7 stores the GTIN/EAN. Blank cells are left untouched. ATTRIBUTE_FILL_MODE
#   (Config.txt) decides whether the file may overwrite a value Cin7 already has.
#   e.g.  SKU, Name, Cost, Barcode
#
#   Creating products (Phase 2): set CREATE_MISSING: True in Config.txt and any SKU
#   in the file that isn't found in Cin7 is CREATED (file mode only). New products
#   take Type / Costing / UOM / Location / accounts / tax / attribute set from the
#   NEW_PRODUCT_* settings in Config.txt; per-row Category, Brand, Barcode,
#   CostingMethod ("Serial" for serial-tracked boilers) and Discount can be columns.
#   Category is required — supply a column or set NEW_PRODUCT_DEFAULT_CATEGORY.
#   e.g.  SKU, Name, Cost, Category, Brand, Barcode, CostingMethod
#
# How to use:
#   1. Place this script, your Credentials.txt, and your price CSV in the same folder
#   2. Set PRICE_FILE_PATH below to match your CSV filename
#   3. Run with DRY_RUN = True first to preview all changes
#   4. If the preview looks correct, set DRY_RUN = False and run again to go live
#
# Retry mode (rerun only the SKUs that errored last time):
#   python PriceUpdaterWithPauseSwitch.py --retry          (retries the most recent log)
#   python PriceUpdaterWithPauseSwitch.py --retry last     (same as above)
#   python PriceUpdaterWithPauseSwitch.py --retry <logfile.csv>
#   - Reads the chosen log, collects every SKU whose Success was False, and reruns
#     only those. It re-reads the SAME source (price file or uplift filter) so the
#     cost and any attribute columns are faithful — failed SKUs no longer in the
#     source are reported and skipped. Writes its own price_update_retry_log_... file.
#
# Credentials.txt format:
#   CIN7_ACCOUNT_ID: your_value_here
#   CIN7_APPLICATION_KEY: your_value_here
#   SIMPRO_CLIENT_ID: your_value_here
#   SIMPRO_CLIENT_SECRET: your_value_here
#   SIMPRO_ACCESS_TOKEN:
#   SHOPIFY_STORE_URL: yourstore.myshopify.com
#   SHOPIFY_ACCESS_TOKEN: shpat_xxxxxxxxxxxxxxxxxxxx
#
# Requirements:
#   pip install requests
# ==============================================================================

import json
import re
import csv
import time
import os
import sys
import requests
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from urllib.parse import quote
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ==============================================================================
# SECTION 01 — Settings
# Change these before running.
# ==============================================================================

# Path to your supplier price CSV file
PRICE_FILE_PATH = os.path.join(SCRIPT_DIR, "Sheet1.csv")

# simPRO instance URL
SIMPRO_BASE_URL = "https://mjryder.simprosuite.com"
SIMPRO_COMPANY_ID = 0

# The vendor name in simPRO that represents RHS Group Ltd as supplier
SIMPRO_SUPPLIER_NAME = "RHS Group Ltd"

# The Cin7 field used to store the pricing audit note
CIN7_INTERNAL_NOTE_FIELD = "InternalNote"

# --- Phase 1: file-driven attribute updates -----------------------------------
# Map an OPTIONAL price-file column (key, matched case-insensitively against the
# CSV header) to the Cin7 product field it should write (value). Add a line per
# attribute you want the file to control. Only columns actually present in the
# file are used; blank cells are skipped. NOTE: in Cin7 Core the GTIN/EAN is
# stored in the product's "Barcode" field — there is no separate GTIN field.
ATTRIBUTE_COLUMN_MAP = {
    "Barcode": "Barcode",
    # "GTIN":        "Barcode",      # alias — writes the same Cin7 field
    # "Category":    "Category",
    # "Brand":       "Brand",
    # "Description": "Description",
    # "UOM":         "UOM",
    # Numeric fields (Weight/Length/Width/Height) can be added later — they need
    # to be sent as numbers, so flag them and I'll wire the conversion.
}


# ==============================================================================
# SECTION 01b — Config (loaded from Config.txt)
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

def _parse_decimal(value, default="0"):
    """Extract a Decimal from plain or messy text. Defined here because the
    Section 05 helpers don't exist yet at config-load time."""
    try:
        m = re.search(r"-?\d+(\.\d+)?", str(value))
        return Decimal(m.group(0)) if m else Decimal(default)
    except Exception:
        return Decimal(default)

def _load_config(filepath="Config.txt"):
    if not os.path.exists(filepath):
        print(f"WARNING: Config.txt not found — using hardcoded defaults.")
        return {}
    cfg = {}
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            cfg[key.strip()] = value.strip()
    return cfg

_cfg = _load_config(os.path.join(SCRIPT_DIR, "Config.txt"))

DRY_RUN            = _parse_bool(_cfg.get("DRY_RUN", "True"))
RATE_LIMIT_PER_MIN = int(_cfg.get("RATE_LIMIT_PER_MIN", "55"))
UPDATE_CIN7        = _parse_bool(_cfg.get("UPDATE_CIN7", "True"))
UPDATE_SIMPRO      = _parse_bool(_cfg.get("UPDATE_SIMPRO", "True"))
UPDATE_SHOPIFY     = _parse_bool(_cfg.get("UPDATE_SHOPIFY", "False"))

# Filters / scope (previously in Config.txt but unused — now read for uplift mode).
# File mode ignores these, so its behaviour is unchanged.
EXCLUDE_BATHROOM_BRANDS = _parse_bool(_cfg.get("EXCLUDE_BATHROOM_BRANDS", "True"))
BRAND_FILTER            = _cfg.get("BRAND_FILTER", "").strip()
CATEGORY_FILTER         = _cfg.get("CATEGORY_FILTER", "").strip()

# Manufacturer uplift mode — bump existing prices by a % instead of reading a file.
UPLIFT_MODE          = _parse_bool(_cfg.get("UPLIFT_MODE", "False"))
UPLIFT_PERCENT       = _parse_decimal(_cfg.get("UPLIFT_PERCENT", "0"))
UPLIFT_SUPPLIER_COST = _parse_bool(_cfg.get("UPLIFT_SUPPLIER_COST", "False"))
REFRESH_CATALOGUE       = _parse_bool(_cfg.get("REFRESH_CATALOGUE", "False"))
CATALOGUE_MAX_AGE_HOURS = int(_parse_decimal(_cfg.get("CATALOGUE_MAX_AGE_HOURS", "24"), "24"))

# Uplift double-run guard: before applying an uplift, fetch the CURRENT prices for
# the matched products (a fresh ~7-min catalogue scan, so a stale cache can't fool
# it) and skip any whose Tier10 already sits ABOVE what their supplier cost would
# justify — i.e. they look already-uplifted. The basis for each product is the cost
# of UPLIFT_DEFAULT_SUPPLIER when that supplier is attached to it, otherwise the cost
# of its highest-priced supplier. If the current Tier10 is above that basis the
# product is skipped; otherwise it gets the uplift. Skipped products are listed in
# the pre-flight so you can sanity-check before the live CONFIRM.
#   UPLIFT_DEFAULT_SUPPLIER — your main buying supplier (e.g. the manufacturer).
#       Blank = always use each product's highest-priced supplier as the basis.
# Set UPLIFT_GUARD: False to skip the check entirely.
UPLIFT_GUARD            = _parse_bool(_cfg.get("UPLIFT_GUARD", "True"))
UPLIFT_DEFAULT_SUPPLIER = _cfg.get("UPLIFT_DEFAULT_SUPPLIER", "").strip()

# Tier audit (--audit) tolerance: a product is only flagged when a tier differs
# from its expected value by BOTH of these — at least AUDIT_TOLERANCE_PERCENT of the
# expected price AND at least AUDIT_TOLERANCE_PENCE in absolute terms. The percent
# stops penny rounding on dear products being flagged; the pence floor stops a 1p
# rounding wobble on a sub-£1 product (where 1p is already >1%) slipping through.
# Genuine drift clears both bars easily.
AUDIT_TOLERANCE_PERCENT = _parse_decimal(_cfg.get("AUDIT_TOLERANCE_PERCENT", "1"), "1")
AUDIT_TOLERANCE_PENCE   = _parse_decimal(_cfg.get("AUDIT_TOLERANCE_PENCE", "2"), "2")

# The audit can be triggered two ways: the --audit command-line switch, or
# AUDIT_MODE here for when flipping a setting is easier than passing an argument
# (running from an IDE, a shortcut, a scheduled task). Either route runs the same
# read-only audit; it never writes, whatever else is set.
AUDIT_MODE          = _parse_bool(_cfg.get("AUDIT_MODE", "False"))
# Equivalent of adding 'priced' after --audit: skip the unpriced (old-markup)
# backlog and report only genuinely mispriced products.
AUDIT_SKIP_UNPRICED = _parse_bool(_cfg.get("AUDIT_SKIP_UNPRICED", "False"))

# How file attributes (Barcode etc.) are applied when Cin7 already has a value:
#   overwrite  = the file always wins (file is the source of truth)  [default]
#   fill_blank = only set the attribute when Cin7's existing value is empty
ATTRIBUTE_FILL_MODE = _cfg.get("ATTRIBUTE_FILL_MODE", "overwrite").strip().lower()
if ATTRIBUTE_FILL_MODE not in ("overwrite", "fill_blank"):
    ATTRIBUTE_FILL_MODE = "overwrite"

# Before any LIVE run, prompt to confirm the Zapier sync Zap(s) are turned OFF,
# so a bulk update doesn't fire single-update Zaps for every changed product.
# Set ZAP_PAUSE_PROMPT: False in Config.txt to skip it. ZAP_NAME just labels the
# message so it reads as the actual Zap you need to pause.
ZAP_PAUSE_PROMPT = _parse_bool(_cfg.get("ZAP_PAUSE_PROMPT", "True"))
ZAP_NAME = _cfg.get("ZAP_NAME", "Cin7 sync Zap(s)").strip() or "Cin7 sync Zap(s)"

# --- Phase 2: create products that don't yet exist in Cin7 ---------------------
# When CREATE_MISSING is True (file mode only), a SKU in the price file that isn't
# found in Cin7 is CREATED instead of erroring. New products need more than a
# price, so the values below fill the Cin7 create-required fields. Per-product
# Category / Brand / Barcode / CostingMethod / Discount / Supplier can be supplied
# as columns in the file; when a column is blank the matching default below is used.
CREATE_MISSING = _parse_bool(_cfg.get("CREATE_MISSING", "False"))

NEW_PRODUCT_TYPE             = _cfg.get("NEW_PRODUCT_TYPE", "Stock").strip() or "Stock"
NEW_PRODUCT_COSTING_METHOD   = _cfg.get("NEW_PRODUCT_COSTING_METHOD", "FIFO").strip() or "FIFO"
NEW_PRODUCT_UOM              = _cfg.get("NEW_PRODUCT_UOM", "Each").strip() or "Each"
NEW_PRODUCT_LOCATION         = _cfg.get("NEW_PRODUCT_LOCATION", "Warehouse").strip() or "Warehouse"
NEW_PRODUCT_INVENTORY_ACCOUNT = _cfg.get("NEW_PRODUCT_INVENTORY_ACCOUNT", "300").strip()
NEW_PRODUCT_COGS_ACCOUNT     = _cfg.get("NEW_PRODUCT_COGS_ACCOUNT", "310").strip()
NEW_PRODUCT_REVENUE_ACCOUNT  = _cfg.get("NEW_PRODUCT_REVENUE_ACCOUNT", "200").strip()
NEW_PRODUCT_PURCHASE_TAX_RULE = _cfg.get("NEW_PRODUCT_PURCHASE_TAX_RULE", "VAT on Expenses").strip()
NEW_PRODUCT_SALE_TAX_RULE    = _cfg.get("NEW_PRODUCT_SALE_TAX_RULE", "VAT on Income").strip()
NEW_PRODUCT_ATTRIBUTE_SET    = _cfg.get("NEW_PRODUCT_ATTRIBUTE_SET", "Product Details").strip()
NEW_PRODUCT_MULTIPLIER       = _parse_decimal(_cfg.get("NEW_PRODUCT_MULTIPLIER", "2"), "2")
NEW_PRODUCT_DROPSHIP         = _cfg.get("NEW_PRODUCT_DROPSHIP", "No Drop Ship").strip() or "No Drop Ship"
# Fallbacks used only when the file row leaves Category / Brand / Discount blank.
NEW_PRODUCT_DEFAULT_CATEGORY = _cfg.get("NEW_PRODUCT_DEFAULT_CATEGORY", "").strip()
NEW_PRODUCT_DEFAULT_BRAND    = _cfg.get("NEW_PRODUCT_DEFAULT_BRAND", "").strip()
NEW_PRODUCT_DISCOUNT         = _cfg.get("NEW_PRODUCT_DISCOUNT", "").strip()
# Optional: attach the file Cost to this supplier on new products. Blank = create
# with correct selling prices but NO supplier-cost row (add via your PO process).
# Must be an existing Cin7 supplier name. A "Supplier" column overrides per row.
NEW_PRODUCT_SUPPLIER         = _cfg.get("NEW_PRODUCT_SUPPLIER", "").strip()

# --- Phase 3: deprecate discontinued products ---------------------------------
# When DEPRECATE_MODE is True the script does NOT update prices. It reads the price
# file as the COMPLETE list for ONE brand, finds every Cin7 product in that brand
# (BRAND_FILTER) that is NOT in the file, and sets those to Deprecated — but only
# if they hold no stock. Anything still in stock (or, optionally, on order) is left
# active and gets retired on a later run once it has sold through. BRAND_FILTER is
# mandatory; the routine refuses to run unscoped. Always dry-run first and review
# the explicit list. A live run writes an undo file you can reverse with
# `--reactivate last`.
DEPRECATE_MODE          = _parse_bool(_cfg.get("DEPRECATE_MODE", "False"))
DEPRECATE_STATUS        = _cfg.get("DEPRECATE_STATUS", "Deprecated").strip() or "Deprecated"
DEPRECATE_HOLD_ON_ORDER = _parse_bool(_cfg.get("DEPRECATE_HOLD_ON_ORDER", "True"))
DEPRECATE_DISPLAY_LIMIT = int(_parse_decimal(_cfg.get("DEPRECATE_DISPLAY_LIMIT", "200"), "200"))


# ==============================================================================
# SECTION 02 — Credentials
# Read from Credentials.txt in the same folder as this script.
# ==============================================================================

def _load_credentials(filepath="Credentials.txt"):
    if not os.path.exists(filepath):
        raise FileNotFoundError(
            f"Credentials file not found: '{filepath}'\n"
            f"Please create Credentials.txt in the same folder as this script."
        )
    creds = {}
    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                print(f"  [Credentials] Skipping unrecognised line {line_num}: {line}")
                continue
            key, _, value = line.partition(":")
            creds[key.strip()] = value.strip()
    return creds

def _require_credential(creds, key):
    value = creds.get(key, "")
    if not value:
        raise ValueError(f"Missing required credential '{key}' in Credentials.txt")
    return value

# Shared credentials live OUTSIDE the project folder (C:\Python), so a single
# file serves every script and can't be committed by accident.
_creds = _load_credentials(r"C:\Python\Credentials.txt")

CIN7_ACCOUNT_ID      = _require_credential(_creds, "CIN7_ACCOUNT_ID")
CIN7_APPLICATION_KEY = _require_credential(_creds, "CIN7_APPLICATION_KEY")
SIMPRO_CLIENT_ID     = _require_credential(_creds, "SIMPRO_CLIENT_ID")
SIMPRO_CLIENT_SECRET = _require_credential(_creds, "SIMPRO_CLIENT_SECRET")
SIMPRO_ACCESS_TOKEN  = _creds.get("SIMPRO_ACCESS_TOKEN", "")  # Blank = auto-refresh
SHOPIFY_STORE_URL    = _require_credential(_creds, "SHOPIFY_STORE_URL")
SHOPIFY_ACCESS_TOKEN = _require_credential(_creds, "SHOPIFY_ACCESS_TOKEN")


# ==============================================================================
# SECTION 03 — API Endpoints and Headers
# ==============================================================================

CIN7_PRODUCT_URL       = "https://inventory.dearsystems.com/ExternalApi/v2/product"
CIN7_MARKUP_PRICES_URL = "https://inventory.dearsystems.com/ExternalApi/v2/product/markupprices"
# Cin7 Core V2 serves product availability under /ExternalApi/v2/ref/productavailability
# — it's a "ref" endpoint, and that missing /ref/ segment is why the earlier paths
# returned Cin7's "Page not found" HTML. The documented path is tried first; the
# probe falls back to older variants if an account/region differs, using the first
# that returns JSON.
CIN7_AVAILABILITY_URLS = [
    "https://inventory.dearsystems.com/ExternalApi/v2/ref/productavailability",
    "https://inventory.dearsystems.com/ExternalApi/ProductAvailability",
    "https://inventory.dearsystems.com/ExternalApi/v2/ProductAvailability",
]
SIMPRO_TOKEN_URL       = f"{SIMPRO_BASE_URL}/oauth2/token"

# Shopify Admin REST API
SHOPIFY_API_URL = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01"
shopify_headers = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type":           "application/json",
    "Accept":                 "application/json"
}

# Create Logs folder if it doesn't exist
os.makedirs(os.path.join(SCRIPT_DIR, "Logs"), exist_ok=True)
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "Logs", f"price_update_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

cin7_headers = {
    "api-auth-accountid":      CIN7_ACCOUNT_ID,
    "api-auth-applicationkey": CIN7_APPLICATION_KEY,
    "Content-Type":            "application/json",
    "Accept":                  "application/json"
}


# ==============================================================================
# SECTION 04 — Rate Limiter
# Tracks API calls and pauses automatically if approaching the 60/minute limit.
# ==============================================================================

class RateLimiter:
    def __init__(self, calls_per_minute):
        self.calls_per_minute = calls_per_minute
        self.call_times = []

    def wait(self):
        now = time.time()
        self.call_times = [t for t in self.call_times if now - t < 60]
        if len(self.call_times) >= self.calls_per_minute:
            sleep_for = 60 - (now - self.call_times[0]) + 0.1
            if sleep_for > 0:
                # Only announce meaningful waits. Sub-0.5s top-ups are just the
                # limiter evenly spacing calls and would otherwise spam the log.
                if sleep_for >= 0.5:
                    print(f"  [Rate limit] Pausing {sleep_for:.1f}s to stay within API limits...")
                time.sleep(sleep_for)
        self.call_times.append(time.time())

rate_limiter = RateLimiter(RATE_LIMIT_PER_MIN)


# ==============================================================================
# SECTION 05 — Pricing Helper Functions
# These match the logic used in the original Zapier pricing script exactly.
# ==============================================================================

def money(value):
    """Round to 2 decimal places using standard rounding."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def to_float(value):
    """Convert a Decimal money value to float for API payloads."""
    return float(money(value))

def clean(value, default=""):
    """Safely convert a value to string, returning default if None."""
    if value is None:
        return default
    return str(value)

def to_decimal_safe(value, default="0"):
    """Safely convert a value to Decimal, extracting the first number if needed."""
    try:
        if value in [None, ""]:
            return Decimal(str(default))
        text = str(value).strip()
        match = re.search(r"-?\d+(\.\d+)?", text)
        if not match:
            return Decimal(str(default))
        return Decimal(match.group(0))
    except Exception:
        return Decimal(str(default))

def format_decimal_clean(value, max_decimal_places=4):
    """Format a Decimal cleanly, stripping trailing zeros (e.g. 2.0000 -> 2)."""
    d = Decimal(str(value)).quantize(
        Decimal("1." + ("0" * max_decimal_places)), rounding=ROUND_HALF_UP
    )
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text

def parse_percent(value, default=0):
    """Extract a percentage value from plain or messy text."""
    if value in [None, ""]:
        return Decimal(str(default))
    match = re.search(r"-?\d+(\.\d+)?", str(value).strip())
    if not match:
        return Decimal(str(default))
    return Decimal(match.group(0))

def format_money_for_note(value):
    """Format a value as a £ money string for audit notes."""
    try:
        return f"\u00a3{money(value):,.2f}"
    except Exception:
        return str(value)

def price_from_margin(cost, margin_percent, do_round=True):
    """
    Calculate selling price from a true gross margin percentage.
    e.g. 10% margin: selling price = cost / 0.90
    do_round=False returns the unrounded value (used by the audit so it can measure
    the true gap rather than one inflated by penny-rounding both sides).
    """
    margin = Decimal(str(margin_percent)) / Decimal("100")
    val = cost / (Decimal("1") - margin)
    return money(val) if do_round else val

def price_from_double_plus(cost, uplift_percent, do_round=True):
    """
    Calculate Tier 1-5 prices using the double-plus-uplift formula.
    Formula: (cost x 2) x (1 + uplift%)
    do_round=False returns the unrounded value (used by the audit so it can measure
    the true gap rather than one inflated by penny-rounding both sides).
    """
    uplift = Decimal(str(uplift_percent)) / Decimal("100")
    val = (cost * Decimal("2")) * (Decimal("1") + uplift)
    return money(val) if do_round else val


# ==============================================================================
# SECTION 06 — Product Name Cleaning
# Fixes encoding issues and normalises product names before writing to Cin7/simPRO.
# ==============================================================================

def fix_encoding_artifacts(value):
    """Fix common mojibake characters that appear in supplier catalogue data."""
    text = clean(value, "")
    replacements = {
        "\u00c2\u00b0":       "\u00b0",
        "\u00c2\u00b0C":      "\u00b0C",
        "\u00c2\u00b0F":      "\u00b0F",
        "\u00c2\u00b1":       "\u00b1",
        "\u00c2\u00ae":       "\u00ae",
        "\u00c2\u00a9":       "\u00a9",
        "\u00c2\u00b5":       "\u00b5",
        "\u00c2\u00b7":       "\u00b7",
        "\u00e2\u0080\u0093": "-",
        "\u00e2\u0080\u0094": "-",
        "\u00e2\u0080\u0098": "'",
        "\u00e2\u0080\u0099": "'",
        "\u00e2\u0080\u009c": '"',
        "\u00e2\u0080\u009d": '"',
        "\u00e2\u0080\u00a2": "\u2022",
        "\u00c3\u00d7":       "x",
        "\u00c2\u00bd":       "1/2",
        "\u00c2\u00bc":       "1/4",
        "\u00c2\u00be":       "3/4",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text

def normalise_units(value):
    """Normalise common measurement units to consistent casing (e.g. MM -> mm, KW -> kW)."""
    text = clean(value, "")
    patterns = [
        (r"\b(\d+(?:\.\d+)?)\s*MM\b",  r"\1mm"),
        (r"\b(\d+(?:\.\d+)?)\s*CM\b",  r"\1cm"),
        (r"\b(\d+(?:\.\d+)?)\s*MTR\b", r"\1mtr"),
        (r"\b(\d+(?:\.\d+)?)\s*M\b",   r"\1m"),
        (r"\b(\d+(?:\.\d+)?)\s*KW\b",  r"\1kW"),
        (r"\b(\d+(?:\.\d+)?)\s*W\b",   r"\1W"),
        (r"\b(\d+(?:\.\d+)?)\s*V\b",   r"\1V"),
        (r"\b(\d+(?:\.\d+)?)\s*DB\b",  r"\1dB"),
        (r"\b(\d+(?:\.\d+)?)\s*L\b",   r"\1L"),
        (r"\b(\d+(?:\.\d+)?)\s*ML\b",  r"\1ml"),
        (r"\b(\d+(?:\.\d+)?)\s*KG\b",  r"\1kg"),
        (r"\b(\d+(?:\.\d+)?)\s*G\b",   r"\1g"),
        (r"\b(\d+(?:\.\d+)?)\s*BAR\b", r"\1bar"),
        (r"\b(\d+(?:\.\d+)?)\s*PSI\b", r"\1psi"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", " x ", text)
    return text

def smart_title_word(word):
    """Title-case a single word while preserving trade acronyms and product codes."""
    if not word:
        return word
    match = re.match(r"^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$", word)
    if not match:
        return word
    prefix, core, suffix = match.groups()
    if not core:
        return word
    preserve_upper = {
        "AC", "DC", "CO", "CO2", "LED", "LCD", "PVC", "ABS", "BSP", "BSPT", "BSPP",
        "WRAS", "IP", "IPX", "RF", "TRV", "PRV", "TMV", "UFH", "PPE", "LPG",
        "ERP", "ERP2", "ERP3", "BS", "EN", "CE", "UKCA", "BEAB", "DIN", "ISO",
        "MCB", "RCD", "RCBO", "MDF", "PTFE", "EPDM", "NBR", "DZR", "HP", "LP",
        "FSC", "PEFC", "T&G", "UV", "RAL", "HDF", "MFC", "TDS", "SDS"
    }
    if core.upper() in preserve_upper:
        return prefix + core.upper() + suffix
    if core.lower() == "x":
        return prefix + "x" + suffix
    if re.search(r"[A-Za-z]", core) and re.search(r"\d", core):
        normed = normalise_units(core)
        return prefix + (normed if normed != core else core) + suffix
    if re.fullmatch(r"\d+(?:\.\d+)?(?:/\d+)?", core):
        return prefix + core + suffix
    return prefix + core[:1].upper() + core[1:].lower() + suffix

def fix_acronyms(value):
    """Final correction pass for trade acronyms that title-casing can damage."""
    text = clean(value, "")
    text = re.sub(r"\bT\s*&\s*G\b", "T&G", text, flags=re.IGNORECASE)
    replacements = {
        "Fsc": "FSC", "Pefc": "PEFC", "Mdf": "MDF", "Hdf": "HDF",
        "Mfc": "MFC", "Pvc": "PVC", "Upvc": "uPVC", "Uv": "UV",
        "Ral": "RAL", "Tds": "TDS", "Sds": "SDS", "Ptfe": "PTFE",
        "Epdm": "EPDM", "Nbr": "NBR", "Dzr": "DZR", "Wras": "WRAS",
        "Beab": "BEAB", "Ukca": "UKCA",
    }
    for bad, good in replacements.items():
        text = re.sub(rf"\b{re.escape(bad)}\b", good, text)
    return text

def clean_product_name(value):
    """Full product name cleanup pipeline: encoding, units, title case, acronyms."""
    text = fix_encoding_artifacts(value)
    text = text.replace('"', "").replace(",", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = normalise_units(text)
    text = fix_acronyms(text)
    text = " ".join(smart_title_word(w) for w in text.split(" "))
    text = normalise_units(text)
    text = fix_acronyms(text)
    return re.sub(r"\s+", " ", text).strip()


# ==============================================================================
# SECTION 07 — Audit Note Builder
# Writes a structured pricing audit note to the Cin7 InternalNote field.
# ==============================================================================

def build_audit_note(sku, name, dry_run, simpro_action, simpro_catalog_id,
                     simpro_new_price, supplier_cost, supplier_name, old_cost,
                     final_tier10, tier10_action, markup_multiplier_used,
                     markup_prices_removed, cost_rule="Bulk price file update", error=""):

    tier10_labels = {
        "increased_from_proposed_multiplier":        "Increased from multiplier rule",
        "unchanged_at_supplier_cost_floor":          "Set/held at supplier cost floor",
        "unchanged_existing_tier10_higher_or_equal": "Left unchanged - existing Tier10 already higher",
        "uplifted_by_percentage":                    "Uplifted by manufacturer percentage",
    }
    simpro_labels = {
        "found_existing": "Updated existing item",
        "created_new":    "Created new item",
    }

    lines = [
        "Pricing Update Audit",
        "====================",
        f"Success: {not bool(error)}",
        f"SKU: {sku}",
        f"Product: {name}",
        f"Mode: {'DRY RUN - no changes made' if dry_run else 'Live update applied'}",
        f"Date: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
        f"Old Cost: {format_money_for_note(old_cost)}",
        f"New Cost: {format_money_for_note(supplier_cost)}",
        f"Cost Rule: {cost_rule}",
        f"Supplier: {supplier_name}",
        "",
        f"Multiplier: {markup_multiplier_used}",
        f"Tier10: {format_money_for_note(final_tier10)}",
        f"Tier10 Action: {tier10_labels.get(str(tier10_action), str(tier10_action))}",
        "",
        f"simPRO Price: {format_money_for_note(simpro_new_price)}",
        f"simPRO Catalog ID: {simpro_catalog_id}",
        f"simPRO Action: {simpro_labels.get(str(simpro_action), str(simpro_action))}",
        "",
        f"Cin7 Markup Rules Removed: {markup_prices_removed}",
    ]

    if error:
        lines += ["", "Error", "-----", str(error)]

    note = "\n".join(lines)
    return note[:1000] + "\n... note truncated ..." if len(note) > 1000 else note


# ==============================================================================
# SECTION 08 — Cin7 API Functions
# ==============================================================================

def cin7_remove_markup_prices(product_id):
    """Remove all markup price rules from a Cin7 product before writing fixed tiers."""
    payload = {
        "ProductID": product_id,
        "MarkupPrices": [{"TierNumber": i, "MarkupType": "D"} for i in range(1, 11)]
    }
    rate_limiter.wait()
    r = requests.put(CIN7_MARKUP_PRICES_URL, headers=cin7_headers,
                     data=json.dumps(payload), timeout=30)
    result = {"ok": 200 <= r.status_code < 300, "status_code": r.status_code}
    try:
        result["response"] = r.json()
    except Exception:
        result["response"] = r.text
    return result


# Cin7 Core's exact CostingMethod values (from the v2 Products API).
_VALID_COSTING_METHODS = {
    "FIFO", "FIFO - Serial Number", "FIFO - Batch",
    "FEFO - Batch", "FEFO - Serial Number",
    "Special - Batch", "Special - Serial Number",
}

def resolve_costing_method(value):
    """Map a friendly file value to a valid Cin7 CostingMethod. Blank -> the
    configured default. 'serial' (any casing) -> 'FIFO - Serial Number' for the
    serial-tracked boilers/filters. An exact valid value is passed through."""
    v = clean(value).strip()
    if not v:
        return NEW_PRODUCT_COSTING_METHOD
    if v in _VALID_COSTING_METHODS:
        return v
    low = v.lower()
    if low in ("serial", "serial number", "fifo serial", "fifo - serial"):
        return "FIFO - Serial Number"
    if low == "batch" or low == "fifo batch":
        return "FIFO - Batch"
    if low == "fifo":
        return "FIFO"
    # Unrecognised — return as-is so Cin7 validates it and reports a clear error
    return v


def cin7_create_product(sku, name, multiplier, category, brand, barcode,
                        costing_method, discount, tiers, supplier_fixed_price):
    """POST a new product to Cin7 with the create-required fields plus the
    configured accounts, tax rules and attribute set. The supplier-cost row and
    audit note are layered on afterwards by the normal update path.
    Returns {ok, product, error}."""
    payload = {
        "SKU":              sku,
        "Name":             name,
        "Description":      name,        # mirror the product name for now
        "ShortDescription": name,        # mirror the product name for now
        "Category":         category,
        "Type":             NEW_PRODUCT_TYPE,
        "CostingMethod":    costing_method,
        "DefaultLocation":  NEW_PRODUCT_LOCATION,
        "UOM":              NEW_PRODUCT_UOM,
        "Status":           "Active",
        "DropShipMode":     NEW_PRODUCT_DROPSHIP,
        "PriceTier1":  tiers["Tier1"],  "PriceTier2":  tiers["Tier2"],
        "PriceTier3":  tiers["Tier3"],  "PriceTier4":  tiers["Tier4"],
        "PriceTier5":  tiers["Tier5"],  "PriceTier6":  tiers["Tier6"],
        "PriceTier7":  tiers["Tier7"],  "PriceTier8":  tiers["Tier8"],
        "PriceTier9":  tiers["Tier9"],  "PriceTier10": tiers["Tier10"],
        "PriceTiers":  {f"Tier {i}": tiers[f"Tier{i}"] for i in range(1, 11)},
        "AttributeSet":         NEW_PRODUCT_ATTRIBUTE_SET,
        "AdditionalAttribute2": format_decimal_clean(multiplier),
        "AdditionalAttribute10": f"{supplier_fixed_price:.2f}",
    }
    # Optional fields — only sent when set, so Cin7 can default anything left blank
    if brand:
        payload["Brand"] = brand
    if barcode:
        payload["Barcode"] = barcode
    if discount:
        payload["DiscountRule"] = discount
    if NEW_PRODUCT_COGS_ACCOUNT:
        payload["COGSAccount"] = NEW_PRODUCT_COGS_ACCOUNT
    if NEW_PRODUCT_INVENTORY_ACCOUNT:
        payload["InventoryAccount"] = NEW_PRODUCT_INVENTORY_ACCOUNT
    if NEW_PRODUCT_REVENUE_ACCOUNT:
        payload["RevenueAccount"] = NEW_PRODUCT_REVENUE_ACCOUNT
    if NEW_PRODUCT_PURCHASE_TAX_RULE:
        payload["PurchaseTaxRule"] = NEW_PRODUCT_PURCHASE_TAX_RULE
    if NEW_PRODUCT_SALE_TAX_RULE:
        payload["SaleTaxRule"] = NEW_PRODUCT_SALE_TAX_RULE

    rate_limiter.wait()
    r = requests.post(CIN7_PRODUCT_URL, headers=cin7_headers,
                      data=json.dumps(payload), timeout=30)
    if not (200 <= r.status_code < 300):
        try:
            err = r.json()           # Cin7 returns a list of validation errors
        except Exception:
            err = r.text[:300]
        return {"ok": False, "error": f"{r.status_code}: {err}"}
    try:
        body = r.json()
    except Exception:
        body = {}
    product = body if isinstance(body, dict) else {}
    return {"ok": True, "product": product}


def validate_new_sku(sku):
    """Return an error string if the SKU breaks Cin7's create rules, else ''."""
    if not sku:
        return "empty SKU"
    if sku != sku.strip():
        return "SKU has leading/trailing spaces"
    digits_only = sku.replace(" ", "")
    if digits_only.isdigit() and digits_only.startswith("0"):
        return "SKU is all-numerals starting with 0 (not allowed by Cin7)"
    if len(sku) > 50:
        return "SKU longer than 50 characters"
    return ""


CATALOGUE_CACHE_PATH = os.path.join(SCRIPT_DIR, "catalogue_index.json")


def cin7_fetch_catalogue_index(page_limit=1000):
    """
    Fetch a lightweight index of ALL products (SKU/Name/Brand/Category).
    Cin7 ignores Brand/Category filters on this endpoint, so we pull the whole
    catalogue once and filter locally. Read-only — no Zapier triggers fire.
    """
    index = []
    page  = 1
    total = None
    while True:
        rate_limiter.wait()
        r = requests.get(CIN7_PRODUCT_URL, headers=cin7_headers,
                         params={"Page": page, "Limit": page_limit}, timeout=30)
        if not (200 <= r.status_code < 300):
            raise ValueError(f"Cin7 product list failed (page {page}): "
                             f"{r.status_code} - {r.text[:300]}")
        body = r.json()
        if isinstance(body, dict):
            products = body.get("Products", []) or []
            total    = body.get("Total", total)
        elif isinstance(body, list):
            products = body
        else:
            products = []
        if not products:
            break
        for p in products:
            sku = clean(p.get("SKU", "")).strip()
            if not sku:
                continue
            index.append({
                "SKU":      sku,
                "Name":     clean(p.get("Name", "")).strip(),
                "Brand":    clean(p.get("Brand", "")).strip(),
                "Category": clean(p.get("Category", "")).strip(),
            })
        print(f"  [catalogue] fetched {len(index)} products...", end="\r")
        if total is not None and page * page_limit >= int(total):
            break
        if len(products) < page_limit:
            break
        page += 1
    print(f"  [catalogue] fetched {len(index)} products total." + " " * 12)
    return index


def load_catalogue_index(refresh=False, max_age_hours=24):
    """
    Return (index, generated_str). Uses the cached index while it is fresher than
    max_age_hours; otherwise rebuilds from Cin7 and re-caches. refresh=True forces
    a rebuild regardless of age.
    """
    if not refresh and os.path.exists(CATALOGUE_CACHE_PATH):
        try:
            with open(CATALOGUE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            idx       = data.get("products", [])
            generated = data.get("generated", "")
            age_hours = None
            try:
                built     = datetime.strptime(generated, "%Y-%m-%d %H:%M")
                age_hours = (datetime.now() - built).total_seconds() / 3600.0
            except Exception:
                pass
            if age_hours is not None and age_hours <= max_age_hours:
                age_str = (f"{age_hours:.0f}h ago" if age_hours >= 1
                           else f"{age_hours * 60:.0f} min ago")
                print(f"  [catalogue] using cached index ({len(idx)} products, "
                      f"built {generated}, {age_str}).")
                return idx, generated
            if age_hours is not None:
                print(f"  [catalogue] cache is {age_hours:.0f}h old "
                      f"(limit {max_age_hours}h) — rebuilding.")
        except Exception as e:
            print(f"  [catalogue] cache unreadable ({e}) — rebuilding.")

    print("  [catalogue] building product index from Cin7 (one-off full scan, ~2-3 min)...")
    idx       = cin7_fetch_catalogue_index()
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(CATALOGUE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"generated": generated, "count": len(idx), "products": idx}, f)
        print(f"  [catalogue] cached to {os.path.basename(CATALOGUE_CACHE_PATH)}.")
    except Exception as e:
        print(f"  [catalogue] WARNING: could not write cache ({e}).")
    return idx, generated


def filter_catalogue(index, brand="", category="", exclude_bathroom_brands=True):
    """
    Client-side filter over the cached index. Returns
    (matched [(sku, name)], brand_match_count, {category: count} for that brand).
    """
    want_brand = brand.strip().lower()
    want_cat   = category.strip().lower()
    matched        = []
    brand_rows     = 0
    cats_for_brand = {}
    for p in index:
        sku     = p.get("SKU", "").strip()
        name    = p.get("Name", "").strip()
        p_brand = p.get("Brand", "").strip().lower()
        p_cat   = p.get("Category", "").strip().lower()
        cat_raw = p.get("Category", "").strip()
        if not sku:
            continue
        if exclude_bathroom_brands and ("bathroom brands" in (p_brand, p_cat)):
            continue
        brand_ok = (not want_brand) or (p_brand == want_brand)
        if brand_ok:
            brand_rows += 1
            cats_for_brand[cat_raw] = cats_for_brand.get(cat_raw, 0) + 1
        cat_ok = (not want_cat) or (p_cat == want_cat)
        if brand_ok and cat_ok:
            matched.append((sku, name))
    return matched, brand_rows, cats_for_brand


def cin7_fetch_pricing_for_skus(wanted_skus, page_limit=1000):
    """
    Page the full catalogue WITH suppliers and capture pricing fields for just the
    SKUs in `wanted_skus`. Returns
        {sku: {"tier10": Decimal|None, "mult_raw": <raw>,
                "suppliers": [{"name": str, "cost": Decimal|None}, ...]}}.

    Always fetched fresh (never cached) so the uplift guard can't be fooled by a
    stale price written by the very run it is trying to protect against. Read-only —
    fires no Zapier workflows. Stops early once every wanted SKU has been seen.
    """
    wanted = {(s or "").strip() for s in wanted_skus if (s or "").strip()}
    if not wanted:
        return {}
    found = {}
    page  = 1
    total = None
    while True:
        rate_limiter.wait()
        r = requests.get(CIN7_PRODUCT_URL, headers=cin7_headers,
                         params={"Page": page, "Limit": page_limit,
                                 "IncludeSuppliers": "true"}, timeout=60)
        if not (200 <= r.status_code < 300):
            raise ValueError(f"Cin7 product list (with suppliers) failed (page {page}): "
                             f"{r.status_code} - {r.text[:300]}")
        body = r.json()
        if isinstance(body, dict):
            products = body.get("Products", []) or []
            total    = body.get("Total", total)
        elif isinstance(body, list):
            products = body
        else:
            products = []
        if not products:
            break
        for p in products:
            sku = clean(p.get("SKU", "")).strip()
            if sku not in wanted:
                continue
            suppliers = []
            for s in (p.get("Suppliers") or []):
                if not isinstance(s, dict):
                    continue
                c = s.get("Cost")
                suppliers.append({
                    "name": clean(s.get("SupplierName", "")).strip(),
                    "cost": (money(to_decimal_safe(c, "0")) if c not in (None, "") else None),
                })
            t10 = p.get("PriceTier10")
            found[sku] = {
                "tier10":    (money(to_decimal_safe(t10, "0")) if t10 not in (None, "") else None),
                "mult_raw":  p.get("AdditionalAttribute2", ""),
                "suppliers": suppliers,
            }
        print(f"  [pricing] scanned page {page}, matched {len(found)}/{len(wanted)}...",
              end="\r")
        if len(found) >= len(wanted):
            break
        if total is not None and page * page_limit >= int(total):
            break
        if len(products) < page_limit:
            break
        page += 1
    print(f"  [pricing] matched {len(found)}/{len(wanted)} of the targeted products."
          + " " * 14)
    return found


def _expected_tier10_from_cost(cost, raw_mult):
    """Expected Tier10 for a supplier cost: cost x multiplier / 2, multiplier floored
    at 2 — identical to file mode's proposed_tier10. Returns Decimal, or None when
    there is no usable cost."""
    if cost is None or cost <= 0:
        return None
    mult = to_decimal_safe(raw_mult, "2")
    if mult < Decimal("2"):
        mult = Decimal("2")
    return money(cost * mult / Decimal("2"))


def classify_uplift_targets(rows, pricing, default_supplier):
    """
    Flag products whose current Tier10 already EXCEEDS what their supplier cost
    justifies, so they look already-uplifted and should be skipped.

    For each product the basis is the cost of `default_supplier` when that supplier
    is attached to it, otherwise the cost of its highest-priced supplier. The
    expected Tier10 is basis x multiplier / 2 (multiplier floored at 2). If the
    current Tier10 is above that, the product is skipped.

    rows            : work tuples (sku, name, ...) as built for the uplift run.
    pricing         : output of cin7_fetch_pricing_for_skus().
    default_supplier: lower-cased supplier-name substring; "" = always use the
                      highest-priced supplier as the basis.

    Returns (skipped, unchecked):
      skipped   = [(sku, name, basis_expected, actual)]   already above basis
      unchecked = [(sku, name, reason)]                   no cost / no Tier10
    Products that are fine to uplift appear in neither list.
    """
    skipped, unchecked = [], []
    for row in rows:
        sku  = row[0]
        name = row[1] if len(row) > 1 else ""
        pr   = pricing.get(sku)
        if pr is None:
            unchecked.append((sku, name, "not returned by the price fetch"))
            continue
        actual = pr.get("tier10")
        if actual is None or actual <= 0:
            unchecked.append((sku, name, "no current Tier10"))
            continue
        suppliers = [s for s in pr.get("suppliers", []) if s.get("cost") and s["cost"] > 0]
        if not suppliers:
            unchecked.append((sku, name, "no supplier cost"))
            continue
        # Basis: default supplier's cost if attached, else the highest-priced supplier.
        basis_cost = None
        if default_supplier:
            named = [s for s in suppliers if default_supplier in s["name"].lower()]
            if named:
                basis_cost = max(named, key=lambda s: s["cost"])["cost"]
        if basis_cost is None:
            basis_cost = max(suppliers, key=lambda s: s["cost"])["cost"]
        expected = _expected_tier10_from_cost(basis_cost, pr.get("mult_raw"))
        if expected is None or expected <= 0:
            unchecked.append((sku, name, "no supplier cost"))
            continue
        if actual > expected:
            skipped.append((sku, name, expected, actual))
        # else: at or below basis -> fine to uplift -> not recorded
    return skipped, unchecked


def cin7_fetch_full_pricing(wanted_skus=None, page_limit=1000):
    """
    Page the full catalogue WITH suppliers and capture what the tier audit needs:
    SKU, Name, Brand, all PriceTier1-10, AdditionalAttribute2, and the supplier rows
    (name + cost). If `wanted_skus` is a set, only those SKUs are kept and paging
    stops once all are found; otherwise every product is kept. Read-only — fires no
    Zapier workflows. Returns a list of dicts.
    """
    wanted = None
    if wanted_skus is not None:
        wanted = {(s or "").strip() for s in wanted_skus if (s or "").strip()}
        if not wanted:
            return []
    out, page, total = [], 1, None
    while True:
        rate_limiter.wait()
        r = requests.get(CIN7_PRODUCT_URL, headers=cin7_headers,
                         params={"Page": page, "Limit": page_limit,
                                 "IncludeSuppliers": "true"}, timeout=60)
        if not (200 <= r.status_code < 300):
            raise ValueError(f"Cin7 product list (audit) failed (page {page}): "
                             f"{r.status_code} - {r.text[:300]}")
        body = r.json()
        if isinstance(body, dict):
            products = body.get("Products", []) or []
            total    = body.get("Total", total)
        elif isinstance(body, list):
            products = body
        else:
            products = []
        if not products:
            break
        for p in products:
            sku = clean(p.get("SKU", "")).strip()
            if wanted is not None and sku not in wanted:
                continue
            suppliers = []
            for s in (p.get("Suppliers") or []):
                if not isinstance(s, dict):
                    continue
                c = s.get("Cost")
                suppliers.append({
                    "name": clean(s.get("SupplierName", "")).strip(),
                    "cost": (money(to_decimal_safe(c, "0")) if c not in (None, "") else None),
                })
            tiers = {}
            for n in range(1, 11):
                v = p.get(f"PriceTier{n}")
                tiers[n] = to_decimal_safe(v, "0") if v not in (None, "") else None
            out.append({
                "sku":       sku,
                "name":      clean(p.get("Name", "")).strip(),
                "brand":     clean(p.get("Brand", "")).strip(),
                "mult_raw":  p.get("AdditionalAttribute2", ""),
                "tiers":     tiers,
                "suppliers": suppliers,
            })
        print(f"  [audit] scanned page {page}, kept {len(out)}...", end="\r")
        if wanted is not None and len({o["sku"] for o in out} & wanted) >= len(wanted):
            break
        if total is not None and page * page_limit >= int(total):
            break
        if len(products) < page_limit:
            break
        page += 1
    print(f"  [audit] fetched {len(out)} products." + " " * 20)
    return out


def _audit_expected_ladder(expected_tier10):
    """Full expected ladder at FULL precision (no rounding) so the audit measures the
    TRUE gap, not one inflated by penny-rounding both sides before comparing."""
    return {
        1:  price_from_double_plus(expected_tier10, 1,   do_round=False),
        2:  price_from_double_plus(expected_tier10, 2,   do_round=False),
        3:  price_from_double_plus(expected_tier10, 3,   do_round=False),
        4:  price_from_double_plus(expected_tier10, 5,   do_round=False),
        5:  price_from_double_plus(expected_tier10, 7.5, do_round=False),
        6:  price_from_margin(expected_tier10, 40, do_round=False),
        7:  price_from_margin(expected_tier10, 30, do_round=False),
        8:  price_from_margin(expected_tier10, 20, do_round=False),
        9:  price_from_margin(expected_tier10, 10, do_round=False),
        10: expected_tier10,
    }


def _audit_one(p, tol_percent=Decimal("1"), tol_pence=Decimal("2")):
    """Classify one fetched product against the expected ladder built from its stored
    supplier cost (highest-priced supplier), ratchet included. A tier counts as off
    only when the gap is BOTH >= tol_percent of the expected price AND >= tol_pence
    (in pence) absolute, so penny rounding never trips it. Returns (status, row):
      ("no_supplier", None) | ("ok", None) | ("mismatch", row_dict)."""
    suppliers = [s for s in p["suppliers"] if s.get("cost") and s["cost"] > 0]
    if not suppliers:
        return ("no_supplier", None)
    basis = max(suppliers, key=lambda s: s["cost"])
    cost  = basis["cost"]
    mult  = to_decimal_safe(p["mult_raw"], "2")
    if mult < Decimal("2"):
        mult = Decimal("2")
    stored   = p["tiers"]
    stored10 = stored.get(10) if stored.get(10) is not None else Decimal("0")
    proposed10 = money(cost * mult / Decimal("2"))
    expected10 = max(cost, stored10, proposed10)
    exp = _audit_expected_ladder(expected10)

    pct_frac  = tol_percent / Decimal("100")
    abs_floor = tol_pence / Decimal("100")     # pence -> pounds
    offs = []
    for n in range(1, 11):
        act = stored.get(n)
        act = act if act is not None else Decimal("0")
        gap = abs(exp[n] - act)
        if gap >= (exp[n] * pct_frac) and gap >= abs_floor:
            offs.append(n)
    if not offs:
        return ("ok", None)

    # Is Tier10 itself materially low (same two-bar test)?
    t10_gap  = expected10 - stored10
    t10_low  = (t10_gap >= (expected10 * pct_frac)) and (t10_gap >= abs_floor)
    if stored10 <= 0:
        status = "Unpriced"      # no fixed tiers (likely still on old markup method)
    elif t10_low:
        status = "UnderPriced"   # Tier10 set but materially below cost basis
    else:
        status = "Drift"         # Tier10 ~correct, but tiers 1-9 don't match it
    row = {
        "SKU": p["sku"], "Name": p["name"], "Brand": p["brand"], "Status": status,
        "Supplier": basis["name"], "SupplierCost": to_float(cost),
        "Multiplier": format_decimal_clean(mult),
        "ExpTier10": to_float(money(exp[10])), "ActTier10": to_float(money(stored10)),
        "Tier10TooLow": "yes" if t10_low else "no",
        "TiersOffCount": len(offs),
        "TiersOff": ",".join(f"T{n}" for n in offs),
    }
    for n in range(1, 11):
        act = stored.get(n)
        row[f"Exp_T{n}"] = to_float(exp[n])
        row[f"Act_T{n}"] = to_float(act if act is not None else Decimal("0"))
    return ("mismatch", row)


def run_audit_mode():
    """
    READ-ONLY tier audit. For every in-scope product that has a supplier, recompute
    the expected price ladder from its stored supplier cost (highest-priced supplier),
    applying the same ratchet and tier formulas as a live re-price, and compare against
    the stored tiers. Writes a CSV of the mismatches. Makes NO changes.
    """
    print("=" * 60)
    print("RHS Group Ltd — Tier Audit (READ-ONLY, no changes made)")
    print("=" * 60)
    print(f"  Tolerance: flag a tier only if it is off by >= "
          f"{format_decimal_clean(AUDIT_TOLERANCE_PERCENT)}% AND "
          f">= {format_decimal_clean(AUDIT_TOLERANCE_PENCE)}p")

    scope_bits = []
    if BRAND_FILTER:    scope_bits.append(f"Brand='{BRAND_FILTER}'")
    if CATEGORY_FILTER: scope_bits.append(f"Category='{CATEGORY_FILTER}'")
    scope_str = " + ".join(scope_bits) if scope_bits else "WHOLE CATALOGUE"
    print(f"  Scope: {scope_str}")

    wanted = None
    if BRAND_FILTER or CATEGORY_FILTER:
        index, _gen = load_catalogue_index(
            refresh=REFRESH_CATALOGUE, max_age_hours=CATALOGUE_MAX_AGE_HOURS)
        matched, _br, _cats = filter_catalogue(
            index, brand=BRAND_FILTER, category=CATEGORY_FILTER,
            exclude_bathroom_brands=EXCLUDE_BATHROOM_BRANDS)
        wanted = {sku for (sku, _n) in matched}
        print(f"  {len(wanted)} product(s) in scope.")
        if not wanted:
            print("  Nothing in scope — check BRAND_FILTER / CATEGORY_FILTER.")
            return

    print("  Fetching current prices (read-only scan; ~7 min for the whole catalogue)...")
    try:
        products = cin7_fetch_full_pricing(wanted)
    except Exception as e:
        print(f"\n  ERROR: price fetch failed ({e}).")
        return

    skip_unpriced = ("priced" in [a.lower() for a in sys.argv[2:]]) or AUDIT_SKIP_UNPRICED
    checked = no_supplier = excluded = mismatched = 0
    n_unpriced = n_underpriced = n_drift = unpriced_hidden = 0
    rows_out = []
    for p in products:
        if (EXCLUDE_BATHROOM_BRANDS and wanted is None
                and p["brand"].strip().lower() == "bathroom brands"):
            excluded += 1
            continue
        status, row = _audit_one(p, AUDIT_TOLERANCE_PERCENT, AUDIT_TOLERANCE_PENCE)
        if status == "no_supplier":
            no_supplier += 1
            continue
        checked += 1
        if status == "ok":
            continue
        mismatched += 1
        st = row["Status"]
        if st == "Unpriced":
            n_unpriced += 1
        elif st == "UnderPriced":
            n_underpriced += 1
        else:
            n_drift += 1
        if skip_unpriced and st == "Unpriced":
            unpriced_hidden += 1
            continue
        rows_out.append(row)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(SCRIPT_DIR, "Logs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"tier_audit_{ts}.csv")
    fix_path = os.path.join(SCRIPT_DIR, f"audit_fix_{ts}.csv")
    if rows_out:
        order = {"UnderPriced": 0, "Drift": 1, "Unpriced": 2}
        rows_out.sort(key=lambda r: (order.get(r["Status"], 9), r["Brand"],
                                     -(r["ExpTier10"] - r["ActTier10"])))
        cols = (["SKU", "Name", "Brand", "Status", "Supplier", "SupplierCost",
                 "Multiplier", "ExpTier10", "ActTier10", "Tier10TooLow",
                 "TiersOffCount", "TiersOff"]
                + [f"Exp_T{n}" for n in range(1, 11)]
                + [f"Act_T{n}" for n in range(1, 11)])
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows_out)
        # Sheet1-format re-price file: feeding each product's CURRENT supplier cost
        # back through normal price-file mode rebuilds the exact ladder shown above
        # (and clears markup). Cost only — no cost change, just a re-price.
        with open(fix_path, "w", newline="", encoding="utf-8-sig") as f:
            fw = csv.writer(f)
            fw.writerow(["SKU", "Name", "Cost", "Category", "Brand", "Barcode",
                         "Discount", "CostingMethod"])
            for r in rows_out:
                fw.writerow([r["SKU"], r["Name"], r["SupplierCost"], "", "", "", "", ""])

    print("\n" + "=" * 60)
    print("  TIER AUDIT RESULTS")
    print("=" * 60)
    print(f"  Scope:                 {scope_str}")
    print(f"  Products fetched:      {len(products)}")
    if excluded:
        print(f"  Bathroom Brands skipped: {excluded}")
    print(f"  With a supplier:       {checked}")
    print(f"  No supplier (skipped): {no_supplier}")
    print(f"  Mismatched (total):    {mismatched}")
    print(f"     Unpriced (no fixed tiers — likely old markup method): {n_unpriced}")
    print(f"     Under-priced (Tier10 below cost basis):               {n_underpriced}")
    print(f"     Selling-tier drift (Tier10 ok, tiers 1-9 off):        {n_drift}")
    if skip_unpriced and unpriced_hidden:
        print(f"  (Excluded {unpriced_hidden} Unpriced from the CSV — '--audit priced'.)")
    if rows_out:
        print(f"\n  First {min(30, len(rows_out))} of {len(rows_out)} written "
              f"(under-priced & drift first):")
        for r in rows_out[:30]:
            print(f"     {r['SKU']:<18} {r['Status']:<11} exp {r['ExpTier10']:>9} "
                  f"act {r['ActTier10']:>9}  {r['Name'][:26]}")
        print(f"\n  Full list written to: {out_path}")
        print(f"  Re-price file (Sheet1 format): {fix_path}")
        print(f"    -> point PRICE_FILE_PATH at it (or rename to Sheet1.csv), "
              f"DRY_RUN first, then run normally to fix them.")
    elif mismatched:
        print("\n  All mismatches were Unpriced and excluded by '--audit priced'.")
    else:
        print("\n  No mismatches found — all in-scope priced products look correct.")
    print("=" * 60)


def _extract_availability_rows(body):
    """ProductAvailability list endpoint wrapper varies; tolerate the common shapes."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("ProductAvailabilityList", "ProductAvailability",
                  "ProductAvailabilities", "Products", "Items"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


def _resolve_availability_url():
    """Cin7's availability endpoint path varies by account/region/API version, and
    a wrong path returns an HTML 'Page not found' with HTTP 200. Probe each known
    candidate with a tiny request and return the first that returns parseable JSON.
    Returns (url, None) on success or (None, [diagnostic lines]) on failure."""
    diags = []
    for url in CIN7_AVAILABILITY_URLS:
        try:
            rate_limiter.wait()
            r = requests.get(url, headers=cin7_headers,
                             params={"Page": 1, "Limit": 1}, timeout=30)
        except Exception as e:
            diags.append(f"    {url} -> request error: {e}")
            continue
        ctype = r.headers.get("Content-Type", "?")
        raw   = (r.text or "").strip()
        if 200 <= r.status_code < 300:
            try:
                r.json()
                return url, None
            except ValueError:
                snippet = raw[:120].replace("\n", " ").replace("\r", "")
                diags.append(f"    {url} -> HTTP 200 but non-JSON ({ctype}): {snippet!r}")
        else:
            diags.append(f"    {url} -> HTTP {r.status_code}: {raw[:120]!r}")
    return None, diags


def cin7_fetch_availability_map(page_limit=1000):
    """
    Build {SKU(upper): {'OnHand': float, 'OnOrder': float}} summed across every
    location/batch row. Cin7's ProductAvailability endpoint ONLY returns rows where
    on-hand / available / on-order are non-zero, so any SKU absent from this map
    holds no stock and has nothing on order — i.e. it is safe to deprecate.
    Read-only — no Zapier triggers fire.
    """
    url, diags = _resolve_availability_url()
    if not url:
        raise ValueError(
            "Could not find a working ProductAvailability endpoint. Tried:\n"
            + "\n".join(diags)
            + "\n        If none worked, tell me which URL returns stock JSON for "
              "your account and I'll lock it in.")
    print(f"  [availability] endpoint: {url}")
    avail = {}
    page  = 1
    total = None
    while True:
        rate_limiter.wait()
        r = requests.get(url, headers=cin7_headers,
                         params={"Page": page, "Limit": page_limit}, timeout=60)
        if not (200 <= r.status_code < 300):
            raise ValueError(f"Cin7 ProductAvailability failed (page {page}): "
                             f"{r.status_code} - {r.text[:300]}")
        try:
            body = r.json()
        except ValueError:
            ctype   = r.headers.get("Content-Type", "?")
            snippet = (r.text or "")[:300].replace("\n", " ")
            raise ValueError(
                f"ProductAvailability returned a non-JSON body on page {page} "
                f"(HTTP {r.status_code}, Content-Type: {ctype}).\n"
                f"        First 300 chars: {snippet!r}")
        if total is None and isinstance(body, dict):
            total = body.get("Total")
        rows = _extract_availability_rows(body)
        if not rows:
            break
        for row in rows:
            sku = clean(row.get("SKU", "")).strip()
            if not sku:
                continue
            slot = avail.setdefault(sku.upper(), {"OnHand": 0.0, "OnOrder": 0.0})
            slot["OnHand"]  += to_float(row.get("OnHand", 0))
            slot["OnOrder"] += to_float(row.get("OnOrder", 0))
        print(f"  [availability] fetched stock for {len(avail)} SKU(s)...", end="\r")
        if total is not None and page * page_limit >= int(total):
            break
        if len(rows) < page_limit:
            break
        page += 1
    print(f"  [availability] {len(avail)} SKU(s) hold stock or have stock on order."
          + " " * 12)
    return avail


# ==============================================================================
# SECTION 09 — simPRO API Functions
# ==============================================================================

def _sp_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def _sp_headers_json(token):
    return {**_sp_headers(token), "Content-Type": "application/json"}

def _sp_parse_error(resp):
    try:
        return resp.json()
    except Exception:
        return resp.text or "(no response body)"

def simpro_refresh_token():
    """Request a new simPRO access token using client credentials."""
    r = requests.post(
        SIMPRO_TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": SIMPRO_CLIENT_ID,
              "client_secret": SIMPRO_CLIENT_SECRET},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise Exception("Token refresh succeeded but access_token missing.")
    return tok

def simpro_validate_token(token):
    """Check whether the current simPRO access token is still valid."""
    if not token:
        return False
    r = requests.get(
        f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}",
        headers=_sp_headers(token), timeout=30,
    )
    return r.status_code == 200

def _sp_extract_items(payload):
    """Extract a list from different possible simPRO response structures."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("Catalogs", "Catalog", "Items", "data", "results",
                  "Records", "Vendors", "Suppliers"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []

def _sp_is_archived(detail):
    """Return True if a simPRO catalogue item appears to be archived/inactive."""
    for k in ("Archived", "archived", "IsArchived", "isArchived",
              "Inactive", "inactive", "IsActive", "isActive", "Active", "active"):
        if k in detail:
            v = detail.get(k)
            if k.lower() in ("isactive", "active"):
                return v is False
            if isinstance(v, bool):
                return v
            if str(v).strip().lower() in ("1", "true", "yes"):
                return True
            if str(v).strip().lower() in ("0", "false", "no"):
                return False
    return False

def simpro_get_catalog_detail(token, catalog_id):
    """Fetch full detail for a simPRO catalogue item (used to check archived status)."""
    url = (f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}"
           f"/catalogs/{quote(str(catalog_id), safe='')}")
    r = requests.get(url, headers=_sp_headers(token), timeout=30)
    if r.status_code != 200:
        return {"ok": False, "status_code": r.status_code, "error": _sp_parse_error(r)}
    data = r.json()
    return {"ok": True, "data": data if isinstance(data, dict) else {}}

def simpro_patch_catalog(token, catalog_id, price, name):
    """Update the name and trade price on a simPRO catalogue item."""
    url = (f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}"
           f"/catalogs/{quote(str(catalog_id), safe='')}")
    payload = {"Name": str(name).strip(), "TradePriceEx": round(price, 2)}
    r = requests.patch(url, json=payload, headers=_sp_headers_json(token), timeout=30)
    if r.status_code in (200, 204):
        return {"ok": True}
    return {"ok": False, "status_code": r.status_code, "error": _sp_parse_error(r)}

def simpro_create_catalog(token, sku, name, price):
    """Create a new catalogue item in simPRO."""
    url = f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}/catalogs/"
    payload = {"PartNo": sku, "Name": name, "TradePriceEx": round(price, 2)}
    r = requests.post(url, json=payload, headers=_sp_headers_json(token), timeout=30)
    if r.status_code not in (200, 201):
        return {"ok": False, "error": _sp_parse_error(r)}
    body = r.json() if r.status_code != 204 else {}
    created_id = (body.get("ID") or body.get("Id") or body.get("id")
                  if isinstance(body, dict) else None)
    if not created_id:
        loc = r.headers.get("Location") or r.headers.get("location")
        if loc:
            created_id = loc.rstrip("/").split("/")[-1]
    return {"ok": True, "created_id": created_id}

def simpro_find_vendor(token, vendor_name):
    """Find a simPRO vendor by exact name match."""
    url = f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}/vendors/"
    r = requests.get(url, headers=_sp_headers(token),
                     params={"search": "any", "Name": vendor_name,
                             "columns": "ID,Name"}, timeout=30)
    if r.status_code != 200:
        return {"ok": False, "error": _sp_parse_error(r)}
    norm = vendor_name.strip().lower()
    exact = [i for i in _sp_extract_items(r.json())
             if isinstance(i, dict)
             and str(i.get("Name", "")).strip().lower() == norm
             and i.get("ID") is not None]
    if not exact:
        return {"ok": False, "error": f"No vendor match for '{vendor_name}'"}
    return {"ok": True, "vendor": exact[0]}

def simpro_update_catalog_vendor(token, catalog_id, vendor_id, vendor_name,
                                  vendor_part_no, nett_price):
    """Add or update the vendor/supplier row on a simPRO catalogue item."""
    cat_safe = quote(str(catalog_id), safe="")
    vendors_url = (f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}"
                   f"/catalogs/{cat_safe}/vendors/")

    # Get existing vendor rows
    r = requests.get(vendors_url, headers=_sp_headers(token), timeout=30)
    if r.status_code != 200:
        return {"ok": False, "error": _sp_parse_error(r)}

    existing_items = _sp_extract_items(r.json())
    existing_row = None
    for item in existing_items:
        v_obj = item.get("Vendor")
        item_id   = v_obj.get("ID") if isinstance(v_obj, dict) else item.get("VendorID") or item.get("ID")
        item_name = v_obj.get("Name", "") if isinstance(v_obj, dict) else item.get("VendorName") or item.get("Name", "")
        if (item_id is not None and str(item_id) == str(vendor_id)) or \
           str(item_name).strip().lower() == vendor_name.strip().lower():
            existing_row = item
            break

    nett = round(float(nett_price), 2)

    if existing_row:
        # PATCH existing row — Vendor field must be excluded
        row_id = (existing_row.get("ID") or existing_row.get("VendorCatalogID") or vendor_id)
        patch_url = f"{vendors_url}{quote(str(row_id), safe='')}"
        patch_payload = {"VendorPartNo": vendor_part_no, "NettPrice": nett, "Default": True}
        pr = requests.patch(patch_url, json=patch_payload,
                            headers=_sp_headers_json(token), timeout=30)
        if pr.status_code in (200, 204):
            return {"ok": True, "action": "updated_existing_vendor"}
        return {"ok": False, "error": _sp_parse_error(pr)}
    else:
        # POST new row — include Vendor ID
        post_payload = {"Vendor": int(vendor_id), "VendorPartNo": vendor_part_no,
                        "NettPrice": nett, "Default": True}
        post = requests.post(vendors_url, json=post_payload,
                             headers=_sp_headers_json(token), timeout=30)
        if post.status_code in (200, 201, 204):
            return {"ok": True, "action": "created_vendor"}
        return {"ok": False, "error": _sp_parse_error(post)}


# ==============================================================================
# SECTION 10 — Shopify API Functions
# Finds a Shopify product variant by SKU and updates price and compare_at_price.
#   price            = Tier4 minus discount rule (same as simPRO price)
#   compare_at_price = Tier4 (full price before discount)
# ==============================================================================

def shopify_find_variant_by_sku(sku):
    """
    Find a Shopify variant by SKU using GraphQL.
    GraphQL with double-quoted SKU is ~6x faster than REST pagination.
    Tested at 0.31s vs 1.95s for a 1,300 product store.
    """
    gql = (
        '{ productVariants(first: 5, query: "sku:\\"' + sku + '\\"") '
        '{ edges { node { id sku price compareAtPrice product { id title } } } } }'
    )
    query = {"query": gql}
    r = requests.post(
        f"{SHOPIFY_API_URL}/graphql.json",
        headers=shopify_headers,
        json=query,
        timeout=30
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"Shopify GraphQL failed: {r.status_code} - {r.text[:200]}"}

    edges = r.json().get("data", {}).get("productVariants", {}).get("edges", [])
    for edge in edges:
        node = edge.get("node", {})
        if str(node.get("sku", "")).strip().lower() == sku.strip().lower():
            return {"ok": True, "variant": node}

    return {"ok": False, "error": f"SKU not found in Shopify: {sku}"}

def shopify_update_variant_price(variant_id, price, compare_at_price):
    numeric_id = str(variant_id).split("/")[-1]
    url = f"{SHOPIFY_API_URL}/variants/{numeric_id}.json"
    payload = {
        "variant": {
            "id":               numeric_id,
            "price":            f"{price:.2f}",
            "compare_at_price": f"{compare_at_price:.2f}"
        }
    }
    r = requests.put(url, headers=shopify_headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        return {"ok": True}
    return {"ok": False, "error": f"Shopify PUT failed: {r.status_code} - {r.text[:200]}"}


# ==============================================================================
# SECTION 11 — Process a Single SKU
# Fetches product from Cin7, calculates new prices, updates Cin7 and simPRO.
# ==============================================================================

def process_sku(sku, file_name, new_supplier_cost, access_token, vendor_info=None, uplift=None, file_attrs=None, create_fields=None):
    """
    Process one SKU from the price file.
    Returns a result dict written to the log CSV.
    access_token is a mutable list [token_string] so it can be refreshed in place.
    file_attrs is an optional {cin7_field: value} dict of attributes from the file
    row (e.g. {"Barcode": "5012345678900"}) to overlay onto the Cin7 product.
    create_fields is an optional {column: value} dict (Category/Brand/CostingMethod/
    Discount/Supplier) used only when CREATE_MISSING is on and the SKU is new.
    """
    result = {
        "SKU": sku, "FileName": file_name, "Action": "",
        "NewSupplierCost": float(new_supplier_cost), "OldSupplierCost": "",
        "Name": "",
        "Tier1": "", "Tier2": "", "Tier3": "", "Tier4": "", "Tier5": "",
        "Tier6": "", "Tier7": "", "Tier8": "", "Tier9": "", "Tier10": "",
        "SimproNewPrice": "", "AttributeChanges": "", "Cin7Updated": False,
        "SimproUpdated": False,
        "ShopifyUpdated": False, "ShopifyPrice": "", "ShopifyCompareAtPrice": "",
        "DryRun": DRY_RUN, "Success": False, "Error": ""
    }

    is_create = False
    create_supplier_name = ""

    try:
        # --- Fetch product from Cin7 ---
        rate_limiter.wait()
        get_resp = requests.get(
            CIN7_PRODUCT_URL, headers=cin7_headers,
            params={"SKU": sku, "IncludeSuppliers": "true"}, timeout=30
        )
        if not (200 <= get_resp.status_code < 300):
            raise ValueError(f"Cin7 GET failed: {get_resp.status_code} - {get_resp.text}")

        data = get_resp.json()
        if isinstance(data, dict) and "Products" in data:
            products = data["Products"]
        elif isinstance(data, dict) and "ProductList" in data:
            products = data["ProductList"]
        elif isinstance(data, list):
            products = data
        else:
            products = [data] if isinstance(data, dict) else []

        if not products:
            # --- SKU isn't in Cin7 ---
            if not (CREATE_MISSING and uplift is None):
                raise ValueError(f"SKU '{sku}' not found in Cin7")

            # CREATE path (file mode only): build a new product from the file row.
            is_create = True
            cf = create_fields or {}
            new_name = clean_product_name(clean(file_name))
            if not new_name:
                raise ValueError(f"Cannot create '{sku}': the file row has no Name")
            sku_err = validate_new_sku(sku)
            if sku_err:
                raise ValueError(f"Cannot create '{sku}': {sku_err}")
            new_category = (cf.get("Category") or NEW_PRODUCT_DEFAULT_CATEGORY).strip()
            if not new_category:
                raise ValueError(
                    f"Cannot create '{sku}': no Category — add a Category column to the "
                    f"file or set NEW_PRODUCT_DEFAULT_CATEGORY in Config.txt")
            new_brand    = (cf.get("Brand") or NEW_PRODUCT_DEFAULT_BRAND).strip()
            new_barcode  = (cf.get("Barcode") or (file_attrs or {}).get("Barcode") or "").strip()
            new_costing  = resolve_costing_method(cf.get("CostingMethod"))
            new_discount = (cf.get("Discount") or NEW_PRODUCT_DISCOUNT).strip()
            new_mult     = max(Decimal("2"), NEW_PRODUCT_MULTIPLIER)
            create_supplier_name = (cf.get("Supplier") or NEW_PRODUCT_SUPPLIER).strip()

            # Price tiers for a new product: cost x multiplier (same maths as updates)
            sc        = money(new_supplier_cost)
            final10_c = max(sc, money(sc * new_mult / Decimal("2")))
            c_tiers = {
                "Tier1":  to_float(price_from_double_plus(final10_c, 1)),
                "Tier2":  to_float(price_from_double_plus(final10_c, 2)),
                "Tier3":  to_float(price_from_double_plus(final10_c, 3)),
                "Tier4":  to_float(price_from_double_plus(final10_c, 5)),
                "Tier5":  to_float(price_from_double_plus(final10_c, 7.5)),
                "Tier6":  to_float(price_from_margin(final10_c, 40)),
                "Tier7":  to_float(price_from_margin(final10_c, 30)),
                "Tier8":  to_float(price_from_margin(final10_c, 20)),
                "Tier9":  to_float(price_from_margin(final10_c, 10)),
                "Tier10": to_float(money(final10_c)),
            }
            fixed_c = to_float(money(final10_c * Decimal("2")))

            if DRY_RUN:
                result["Action"] = "would_create"
                result["Name"]   = new_name
                result.update(c_tiers)
                result["Success"] = True
                print(f"  [DRY RUN] CREATE {sku}: {new_name} | Cat: {new_category} "
                      f"| Brand: {new_brand or '-'} | Costing: {new_costing} "
                      f"| Tier10: \u00a3{c_tiers['Tier10']}"
                      + (f" | Barcode: {new_barcode}" if new_barcode else "")
                      + (f" | Supplier: {create_supplier_name}" if create_supplier_name else " | (no supplier cost row)"))
                return result

            # Live: POST to create, then fall through to the normal update path so
            # the supplier cost, audit note and simPRO item are layered on.
            created = cin7_create_product(
                sku=sku, name=new_name, multiplier=new_mult,
                category=new_category, brand=new_brand, barcode=new_barcode,
                costing_method=new_costing, discount=new_discount,
                tiers=c_tiers, supplier_fixed_price=fixed_c)
            if not created.get("ok"):
                raise ValueError(f"Cin7 create failed: {created.get('error')}")
            product = created.get("product") or {}
            if not product.get("ID"):
                # Re-fetch to get a complete product structure with its new ID
                rate_limiter.wait()
                rg = requests.get(CIN7_PRODUCT_URL, headers=cin7_headers,
                                  params={"SKU": sku, "IncludeSuppliers": "true"}, timeout=30)
                rgj = rg.json() if 200 <= rg.status_code < 300 else {}
                plist = (rgj.get("Products") if isinstance(rgj, dict) else None) or \
                        (rgj if isinstance(rgj, list) else [])
                if plist:
                    product = plist[0]
            if not product.get("ID"):
                raise ValueError(f"Created '{sku}' but could not read it back (no ID returned)")
            products = [product]
            result["Action"] = "created"

        if not result["Action"]:
            result["Action"] = "would_update" if DRY_RUN else "updated"

        product    = products[0]
        product_id = product.get("ID")
        if not product_id:
            raise ValueError("Cin7 response missing product ID")

        name = clean_product_name(clean(product.get("Name", "")))
        result["Name"] = name

        # --- Record old supplier cost ---
        existing_suppliers = product.get("Suppliers", [])
        old_cost = Decimal("0")
        supplier_name = ""
        if isinstance(existing_suppliers, list) and existing_suppliers:
            old_cost      = money(to_decimal_safe(existing_suppliers[0].get("Cost"), "0"))
            supplier_name = clean(existing_suppliers[0].get("SupplierName", ""))
        result["OldSupplierCost"] = float(old_cost)

        # --- Read markup multiplier from AdditionalAttribute2 (minimum 2) ---
        raw_multiplier    = product.get("AdditionalAttribute2", "")
        markup_multiplier = to_decimal_safe(raw_multiplier, "2")
        if markup_multiplier < Decimal("2"):
            markup_multiplier = Decimal("2")
        multiplier_str = format_decimal_clean(markup_multiplier)

        if uplift:
            # --- Manufacturer uplift mode (no price file) ---
            # Lift the existing Tier10 anchor by the percentage and recompute every
            # tier from it. The file-mode max() ratchet is deliberately bypassed so a
            # deliberate increase always lands on every matched line.
            pct        = Decimal(str(uplift["percent"]))
            factor     = Decimal("1") + (pct / Decimal("100"))
            raw_anchor = to_decimal_safe(product.get("PriceTier10"), "0")
            if raw_anchor <= 0:
                raise ValueError(
                    f"SKU '{sku}' has no existing Tier10 to uplift — "
                    f"skipped (uplift mode only moves products that are already priced)"
                )
            existing_tier10 = money(raw_anchor)
            final_tier10    = money(existing_tier10 * factor)

            if uplift.get("supplier_cost"):
                supplier_cost = money(old_cost * factor)
            else:
                supplier_cost = money(old_cost)   # held — buy cost waits for the real price file

            tier10_action = "uplifted_by_percentage"
            cost_rule     = uplift["label"]
        else:
            # --- File mode: cost-driven from the CSV (unchanged behaviour) ---
            supplier_cost = money(new_supplier_cost)

            # Tier10 = max of: supplier cost, existing Tier10, proposed (cost x multiplier / 2)
            existing_tier10 = money(to_decimal_safe(product.get("PriceTier10"), str(supplier_cost)))
            if existing_tier10 < supplier_cost:
                existing_tier10 = supplier_cost
            proposed_tier10 = money(supplier_cost * markup_multiplier / Decimal("2"))
            final_tier10    = max(supplier_cost, existing_tier10, proposed_tier10)

            if final_tier10 > existing_tier10:
                tier10_action = "increased_from_proposed_multiplier"
            elif existing_tier10 == supplier_cost and proposed_tier10 <= existing_tier10:
                tier10_action = "unchanged_at_supplier_cost_floor"
            else:
                tier10_action = "unchanged_existing_tier10_higher_or_equal"
            cost_rule = "Bulk price file update"

        result["NewSupplierCost"] = float(supplier_cost)
        cost  = final_tier10
        tiers = {
            "Tier1":  to_float(price_from_double_plus(cost, 1)),
            "Tier2":  to_float(price_from_double_plus(cost, 2)),
            "Tier3":  to_float(price_from_double_plus(cost, 3)),
            "Tier4":  to_float(price_from_double_plus(cost, 5)),
            "Tier5":  to_float(price_from_double_plus(cost, 7.5)),
            "Tier6":  to_float(price_from_margin(cost, 40)),
            "Tier7":  to_float(price_from_margin(cost, 30)),
            "Tier8":  to_float(price_from_margin(cost, 20)),
            "Tier9":  to_float(price_from_margin(cost, 10)),
            "Tier10": to_float(money(cost)),
        }
        result.update(tiers)

        supplier_fixed_price = to_float(money(cost * Decimal("2")))

        # simPRO price = Tier4 minus discount rule (minimum 40%)
        discount_rule    = parse_percent(product.get("DiscountRule"), 0)
        discount_used    = max(Decimal("40"), discount_rule)
        simpro_price     = money(Decimal(str(tiers["Tier4"])) * (Decimal("1") - discount_used / Decimal("100")))
        simpro_price_f   = to_float(simpro_price)
        result["SimproNewPrice"] = simpro_price_f

        # --- Build Cin7 supplier payload with updated Cost ---
        supplier_payload = []
        if isinstance(existing_suppliers, list):
            for s in existing_suppliers:
                supplier_payload.append({
                    "SupplierID":           s.get("SupplierID"),
                    "SupplierName":         clean(s.get("SupplierName")),
                    "SupplierInventoryCode": clean(s.get("SupplierInventoryCode")),
                    "SupplierProductName":  clean_product_name(s.get("SupplierProductName") or name),
                    "Cost":                 float(supplier_cost),   # Updated from price file
                    "FixedCost":            supplier_fixed_price,
                    "Currency":             s.get("Currency"),
                    "DropShip":             bool(s.get("DropShip", False)),
                    "URL":                  clean(s.get("URL") or s.get("SupplierProductURL")),
                })

        # New product with a configured supplier: attach the file Cost to it so the
        # buy price is recorded (the supplier name must already exist in Cin7).
        if is_create and not supplier_payload and create_supplier_name:
            supplier_payload.append({
                "SupplierName":         create_supplier_name,
                "SupplierInventoryCode": "",
                "SupplierProductName":  name,
                "Cost":                 float(supplier_cost),
                "FixedCost":            supplier_fixed_price,
                "DropShip":             False,
                "URL":                  "",
            })
            supplier_name = create_supplier_name

        # --- Build Cin7 PUT payload ---
        cin7_payload = {
            "ID":               product_id,
            "Name":             name,
            "ShortDescription": name,
            "PriceTier1":       tiers["Tier1"],  "PriceTier2":  tiers["Tier2"],
            "PriceTier3":       tiers["Tier3"],  "PriceTier4":  tiers["Tier4"],
            "PriceTier5":       tiers["Tier5"],  "PriceTier6":  tiers["Tier6"],
            "PriceTier7":       tiers["Tier7"],  "PriceTier8":  tiers["Tier8"],
            "PriceTier9":       tiers["Tier9"],  "PriceTier10": tiers["Tier10"],
            "PriceTiers":       {f"Tier {i}": tiers[f"Tier{i}"] for i in range(1, 11)},
            "AttributeSet":          product.get("AttributeSet"),
            "AdditionalAttribute1":  product.get("AdditionalAttribute1", ""),
            "AdditionalAttribute2":  multiplier_str,
            "AdditionalAttribute3":  product.get("AdditionalAttribute3", ""),
            "AdditionalAttribute4":  product.get("AdditionalAttribute4", ""),
            "AdditionalAttribute5":  product.get("AdditionalAttribute5", ""),
            "AdditionalAttribute6":  product.get("AdditionalAttribute6", ""),
            "AdditionalAttribute7":  product.get("AdditionalAttribute7", ""),
            "AdditionalAttribute8":  product.get("AdditionalAttribute8", ""),
            "AdditionalAttribute9":  product.get("AdditionalAttribute9", ""),
            "AdditionalAttribute10": f"{supplier_fixed_price:.2f}",
            # The audit note is deliberately NOT written in this main PUT. It is
            # written once, after the simPRO/Shopify sync, by the note-only PUT
            # below (so it can record the real simPRO action / catalog id). Writing
            # a provisional copy here as well was redundant - two PUTs per product.
            # A mid-sync failure is still captured in the run-log CSV by the except
            # handler at the end of process_sku, so no error information is lost.
        }
        if supplier_payload:
            cin7_payload["Suppliers"] = supplier_payload

        # --- Overlay file-driven attributes (Barcode/GTIN etc.) ---
        # Cin7's product PUT is a merge, not a replace: the note-only PUT further
        # down sends just {ID, InternalNote} and has never wiped the rest of the
        # product in production, which proves omitted fields are preserved. So we
        # only set a field when the file actually supplies a value; everything we
        # don't mention is left exactly as Cin7 has it. ATTRIBUTE_FILL_MODE decides
        # whether a file value may overwrite a value Cin7 already holds.
        attr_changes = {}
        if file_attrs:
            for cin7_field, supplied_val in file_attrs.items():
                supplied_val = clean(supplied_val).strip()
                if not supplied_val:
                    continue                      # blank cell — leave Cin7 as-is
                existing_val = clean(product.get(cin7_field, "")).strip()
                if ATTRIBUTE_FILL_MODE == "fill_blank" and existing_val:
                    continue                      # don't overwrite an existing value
                if supplied_val == existing_val:
                    continue                      # already correct — no change
                cin7_payload[cin7_field] = supplied_val
                attr_changes[cin7_field] = f"{existing_val or '(blank)'} -> {supplied_val}"
        result["AttributeChanges"] = " | ".join(f"{k}: {v}" for k, v in attr_changes.items())

        # --- Dry run: print preview and stop ---
        if DRY_RUN:
            print(f"  [DRY RUN] SKU {sku}: Cost \u00a3{old_cost} -> \u00a3{supplier_cost} "
                  f"| Tier10: \u00a3{tiers['Tier10']} | Price: \u00a3{simpro_price_f} | Compare at: \u00a3{tiers['Tier4']}")
            if attr_changes:
                print(f"            Attributes: " + " | ".join(f"{k}: {v}" for k, v in attr_changes.items()))
            result["ShopifyPrice"]          = simpro_price_f
            result["ShopifyCompareAtPrice"] = tiers["Tier4"]
            result["Success"] = True
            return result

        # --- Live: remove Cin7 markup rules and update ---
        if UPDATE_CIN7:
            markup_result = cin7_remove_markup_prices(product_id)
            if not markup_result.get("ok"):
                raise ValueError(f"Cin7 markup removal failed: {markup_result.get('response')}")

            rate_limiter.wait()
            put_resp = requests.put(CIN7_PRODUCT_URL, headers=cin7_headers,
                                    data=json.dumps(cin7_payload), timeout=30)
            if not (200 <= put_resp.status_code < 300):
                raise ValueError(f"Cin7 PUT failed: {put_resp.status_code} - {put_resp.text}")
            result["Cin7Updated"] = True

        # --- Live: find or create simPRO catalogue item ---
        catalog_id = ""
        action     = ""
        if UPDATE_SIMPRO:
            sku_safe   = quote(sku, safe="")
            cols       = quote("ID,PartNo,Name,TradePriceEx", safe=",")
            search_url = (f"{SIMPRO_BASE_URL}/api/v1.0/companies/{SIMPRO_COMPANY_ID}"
                          f"/catalogs/?search=any&PartNo={sku_safe}&columns={cols}")
            sr = requests.get(search_url, headers=_sp_headers(access_token[0]), timeout=30)
            if sr.status_code == 401:
                access_token[0] = simpro_refresh_token()
                sr = requests.get(search_url, headers=_sp_headers(access_token[0]), timeout=30)
            if sr.status_code != 200:
                raise ValueError(f"simPRO catalogue search failed: {sr.status_code}")

            sku_norm   = sku.lower()
            candidates = [i for i in _sp_extract_items(sr.json())
                          if isinstance(i, dict)
                          and str(i.get("PartNo", "")).strip().lower() == sku_norm
                          and i.get("ID") is not None]
            try:
                candidates = sorted(candidates, key=lambda x: int(x.get("ID", 0)), reverse=True)
            except Exception:
                pass

            chosen = None
            for c in candidates[:10]:
                d = simpro_get_catalog_detail(access_token[0], c.get("ID"))
                if d.get("ok") and not _sp_is_archived(d.get("data", {})):
                    chosen = c
                    break

            if not chosen:
                created = simpro_create_catalog(access_token[0], sku, name, simpro_price_f)
                if not created.get("ok"):
                    raise ValueError(f"simPRO create failed: {created.get('error')}")
                catalog_id = created.get("created_id")
                action     = "created_new"
            else:
                catalog_id = chosen.get("ID")
                action     = "found_existing"

            patch = simpro_patch_catalog(access_token[0], catalog_id, simpro_price_f, name)
            if not patch.get("ok"):
                raise ValueError(f"simPRO PATCH failed: {patch.get('error')}")

            if vendor_info is None:
                raise ValueError(f"simPRO vendor info not available — check '{SIMPRO_SUPPLIER_NAME}' exists in simPRO")
            vendor_result = simpro_update_catalog_vendor(
                token=access_token[0], catalog_id=catalog_id,
                vendor_id=vendor_info["ID"],
                vendor_name=SIMPRO_SUPPLIER_NAME,
                vendor_part_no=sku, nett_price=simpro_price_f
            )
            if not vendor_result.get("ok"):
                raise ValueError(f"simPRO vendor update failed: {vendor_result.get('error')}")

            result["SimproUpdated"] = True

        # --- Live: write final audit note to Cin7 ---
        if UPDATE_CIN7:
            final_note = build_audit_note(
                sku=sku, name=name, dry_run=False,
                simpro_action=action, simpro_catalog_id=catalog_id,
                simpro_new_price=simpro_price_f,
                supplier_cost=float(supplier_cost),
                supplier_name=supplier_name,
                old_cost=float(old_cost),
                final_tier10=float(final_tier10),
                tier10_action=tier10_action,
                markup_multiplier_used=multiplier_str,
                markup_prices_removed=True,
                cost_rule=cost_rule,
            )
            rate_limiter.wait()
            requests.put(CIN7_PRODUCT_URL, headers=cin7_headers,
                         data=json.dumps({"ID": product_id,
                                          CIN7_INTERNAL_NOTE_FIELD: final_note}),
                         timeout=30)

        # --- Live: update Shopify variant price ---
        if UPDATE_SHOPIFY:
            shopify_lookup = shopify_find_variant_by_sku(sku)
            if shopify_lookup.get("ok"):
                shopify_result = shopify_update_variant_price(
                    variant_id       = shopify_lookup["variant"]["id"],
                    price            = simpro_price_f,
                    compare_at_price = tiers["Tier4"]
                )
                if shopify_result.get("ok"):
                    result["ShopifyUpdated"]        = True
                    result["ShopifyPrice"]          = simpro_price_f
                    result["ShopifyCompareAtPrice"] = tiers["Tier4"]
                    shopify_status = f"\u00a3{simpro_price_f}"
                else:
                    result["Error"] = f"Shopify: {shopify_result.get('error', '')}"
                    shopify_status = "failed"
            else:
                shopify_status = "not in Shopify"
        else:
            shopify_status = "skipped"

        # simPRO status mirrors the Shopify line: show the price only when it was
        # actually pushed to simPRO. When simPRO is off (or, defensively, didn't
        # update) show "skipped" with the calculated price in brackets, so the
        # figure is never mistaken for a value that was sent.
        if not UPDATE_SIMPRO:
            simpro_status = f"skipped (\u00a3{simpro_price_f})"
        elif result["SimproUpdated"]:
            simpro_status = f"\u00a3{simpro_price_f}"
        else:
            simpro_status = f"not updated (\u00a3{simpro_price_f})"

        result["Success"] = True
        tag = "CREATED " if is_create else "[OK]"
        print(f"  {tag} SKU {sku}: Cost \u00a3{old_cost} -> \u00a3{supplier_cost} "
              f"| Tier10: \u00a3{tiers['Tier10']} | Simpro: {simpro_status} | Shopify: {shopify_status}")
        if attr_changes:
            print(f"         Attributes set: " + " | ".join(f"{k}: {v}" for k, v in attr_changes.items()))

    except Exception as e:
        result["Error"]   = str(e)
        result["Success"] = False
        print(f"  [ERROR] SKU {sku}: {e}")

    return result


# ==============================================================================
# SECTION 11b — Retry Support (rerun only the SKUs that errored last time)
# ==============================================================================

def find_latest_log():
    """Return the path of the most recent price-update log in the Logs folder, or
    None. Matches both 'price_update_log_*' and 'price_update_retry_log_*' so
    'retry last' chains: a retry of a retry re-runs whatever still failed."""
    log_dir = os.path.join(SCRIPT_DIR, "Logs")
    if not os.path.isdir(log_dir):
        return None
    logs = [
        os.path.join(log_dir, f)
        for f in os.listdir(log_dir)
        if f.startswith("price_update_") and f.endswith(".csv")
    ]
    if not logs:
        return None
    return max(logs, key=os.path.getmtime)


def load_error_skus_from_log(filepath):
    """Read a previous run's log CSV and return the SKUs whose Success was not
    truthy (errors + interruptions). Order preserved, duplicates removed."""
    error_skus, seen = [], set()
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sku = (row.get("SKU") or "").strip()
            ok  = str(row.get("Success", "")).strip().lower() in ("true", "1", "yes")
            if sku and not ok and sku not in seen:
                error_skus.append(sku)
                seen.add(sku)
    return error_skus


def _parse_retry_arg():
    """Detect retry mode from the command line.

    Usage:
        python PriceUpdaterWithPauseSwitch.py                 -> normal run
        python PriceUpdaterWithPauseSwitch.py --retry         -> retry the latest log
        python PriceUpdaterWithPauseSwitch.py --retry last    -> retry the latest log
        python PriceUpdaterWithPauseSwitch.py --retry <file>  -> retry a named log

    Returns (retry_mode: bool, log_path: str | None).
    """
    args = sys.argv[1:]
    if args and args[0].lower() in ("--retry", "-r", "retry"):
        log_path = args[1] if len(args) > 1 else None
        return True, log_path
    return False, None


def _apply_retry_filter(rows, error_skus):
    """Keep only rows whose SKU is in error_skus (retry mode), preserving order.
    Reports any failed SKUs that aren't in the current source so they're not lost
    silently. rows are (sku, name, cost, attrs) tuples."""
    kept    = [r for r in rows if r[0] in error_skus]
    found   = {r[0] for r in kept}
    missing = [s for s in error_skus if s not in found]
    print(f"\nRETRY MODE: {len(error_skus):,} failed SKU(s) in the source log; "
          f"{len(kept):,} found in the current input ({len(rows):,} scanned).")
    if missing:
        print(f"  {len(missing):,} failed SKU(s) are not in the current input and "
              f"can't be retried this way (changed/removed from the source):")
        for s in missing[:10]:
            print(f"        - {s}")
        if len(missing) > 10:
            print(f"        ...and {len(missing) - 10:,} more")
    return kept


def confirm_proceed(prompt="Proceed?"):
    """Ask a [Y/N] question in the console. Returns True only on an explicit yes
    (y / yes). Anything else — including Enter, N, or Ctrl+C — returns False so the
    safe default is always to NOT proceed."""
    try:
        resp = input(f"  {prompt} [Y/N] > ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return resp in ("y", "yes")


def confirm_typed(proceed_word="CONFIRM"):
    """Require the operator to type a specific word (default CONFIRM) to proceed.
    Pressing Enter on an empty line cancels; ANY other entry re-prompts, so a stray
    keystroke (e.g. 'y') can't abandon the run by accident. Ctrl+C / EOF cancels.
    Returns True to proceed, False to cancel."""
    while True:
        try:
            resp = input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            return False
        if resp == "":
            return False
        if resp.upper() == proceed_word.upper():
            return True
        print(f"  Please type {proceed_word} to proceed, or press Enter to cancel.")


def confirm_zap_paused():
    """Live-run safety: remind the operator to pause the Zapier sync Zap(s) before
    a bulk update fires single-update Zaps for every changed product. Returns True
    to proceed, False to abort. Skipped when ZAP_PAUSE_PROMPT is off in Config.txt."""
    if not ZAP_PAUSE_PROMPT:
        return True
    print()
    print("  " + "-" * 56)
    print(f"  ZAPIER: a bulk update will trigger your '{ZAP_NAME}' for")
    print(f"  every changed product unless it is turned OFF first.")
    print("  " + "-" * 56)
    return confirm_proceed(f"Is the '{ZAP_NAME}' turned OFF?")


# ==============================================================================
# SECTION 12 — Main Runner
# Reads the price file, processes each SKU, writes the log CSV.
# ==============================================================================

def check_business_hours():
    """
    Warns if the script is being run during business hours (Mon-Fri 06:00-17:00).
    Bulk updates during business hours will trigger Zapier single-update workflows
    for every product changed, consuming thousands of Zapier tasks.
    Dry runs are always allowed regardless of time.
    """
    # Use local system time — your PC is set to UK time so this is reliable
    now       = datetime.now()
    weekday   = now.weekday()   # 0=Monday, 6=Sunday
    hour      = now.hour
    minute    = now.minute
    time_str  = now.strftime("%H:%M")
    day_str   = now.strftime("%A")

    is_business_hours = (weekday <= 4) and (8 <= hour < 17)

    if not is_business_hours:
        return True  # Outside business hours — safe to proceed

    print()
    print("=" * 60)
    print("  *** BUSINESS HOURS WARNING ***")
    print("=" * 60)
    print(f"  Current time: {day_str} {time_str} (UK)")
    print(f"  Business hours: Monday-Friday 08:00-17:00")
    print()
    print("  Running bulk updates during business hours will trigger")
    print("  your Zapier single-update workflows for EVERY product")
    print("  changed, consuming thousands of Zapier tasks.")
    print()
    print("  Recommended: run bulk updates outside business hours.")
    print()
    print("  Type CONFIRM to proceed anyway, or press Enter to cancel:")
    print("=" * 60)

    if confirm_typed():
        print()
        print("  Proceeding during business hours — Zapier tasks will be consumed.")
        print()
        return True
    else:
        print()
        print("  Cancelled. Please re-run outside business hours (Mon-Fri before 06:00 or after 17:00).")
        print()
        return False


# ==============================================================================
# SECTION 11b — Phase 3: Deprecate discontinued products (and reactivate/undo)
# Reads the price file as the COMPLETE list for one brand, deprecates in-brand
# products that aren't in the file AND hold no stock. Brand-scoped or it refuses.
# ==============================================================================

DEPRECATE_LOG_FIELDS = ["SKU", "Name", "Action", "OnHand", "OnOrder",
                        "PriorStatus", "NewStatus", "Note", "DryRun", "Success", "Error"]


def _depr_row(sku, name, action, onhand="", onorder="", prior_status="",
              new_status="", note="", success=False, error=""):
    return {"SKU": sku, "Name": name, "Action": action, "OnHand": onhand,
            "OnOrder": onorder, "PriorStatus": prior_status, "NewStatus": new_status,
            "Note": note, "DryRun": DRY_RUN, "Success": success, "Error": error}


def _write_deprecate_log(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DEPRECATE_LOG_FIELDS)
        w.writeheader()
        w.writerows(rows)


def _write_undo_file(path, rows):
    fields = ["SKU", "Name", "PriorStatus", "ProductID", "DeprecatedAt"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def load_file_skus(filepath):
    """Return the SET of SKUs in the price file (upper-cased) — the 'keep' list.
    Unlike the price-update parser this keeps any row that has a SKU even if Cost
    is blank: a SKU that appears in the supplier file is in range and must never
    be deprecated."""
    skus = set()
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sku = (row.get("SKU") or "").strip()
            if sku:
                skus.add(sku.upper())
    return skus


def cin7_get_product_by_sku(sku):
    """GET one product by SKU. Returns the product dict or None. Read-only."""
    rate_limiter.wait()
    r = requests.get(CIN7_PRODUCT_URL, headers=cin7_headers,
                     params={"SKU": sku}, timeout=30)
    if not (200 <= r.status_code < 300):
        raise ValueError(f"Cin7 GET failed for {sku}: {r.status_code} - {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict) and "Products" in data:
        products = data["Products"]
    elif isinstance(data, dict) and "ProductList" in data:
        products = data["ProductList"]
    elif isinstance(data, list):
        products = data
    else:
        products = [data] if isinstance(data, dict) else []
    return products[0] if products else None


def cin7_set_product_status(product_id, status):
    """PUT a product's Status only (Cin7 merges, so untouched fields are kept).
    Returns (ok: bool, error_text: str)."""
    rate_limiter.wait()
    r = requests.put(CIN7_PRODUCT_URL, headers=cin7_headers,
                     data=json.dumps({"ID": product_id, "Status": status}),
                     timeout=30)
    if 200 <= r.status_code < 300:
        return True, ""
    return False, f"{r.status_code} - {r.text[:300]}"


def run_deprecate_mode():
    """Brand-scoped deprecation of products that have dropped off the supplier file
    and hold no stock. Dry-run previews the explicit list; live runs are gated and
    write an undo file."""
    global LOG_FILE_PATH
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "Logs", f"deprecate_log_{ts}.csv")
    undo_path     = os.path.join(SCRIPT_DIR, "Logs", f"deprecate_undo_{ts}.csv")

    print("=" * 60)
    print("RHS Group Ltd — Deprecate Discontinued Products")
    print(f"Mode:       {'DRY RUN (no changes will be made)' if DRY_RUN else '*** LIVE — products will be DEPRECATED ***'}")
    print(f"Price file: {PRICE_FILE_PATH}")
    print(f"Log file:   {LOG_FILE_PATH}")
    print("=" * 60)

    # --- Guard 1: must be brand-scoped. Never run across the whole catalogue. ---
    if not BRAND_FILTER:
        print("\nERROR: DEPRECATE_MODE is on but BRAND_FILTER is blank.")
        print("This routine retires every product in a brand that isn't in the price")
        print("file. With no brand it would deprecate almost your entire catalogue.")
        print("Set BRAND_FILTER in Config.txt to the single brand this file covers.")
        return

    # --- Guard 2: the 'keep' list — every SKU in the supplier file ---
    try:
        keep = load_file_skus(PRICE_FILE_PATH)
    except FileNotFoundError:
        print(f"\nERROR: Price file not found at '{PRICE_FILE_PATH}'")
        return
    if not keep:
        print("\nERROR: No SKUs found in the price file. Refusing to run — an empty")
        print("file would mark the entire brand for deprecation.")
        return

    scope_bits = [f"Brand='{BRAND_FILTER}'"]
    if CATEGORY_FILTER:
        scope_bits.append(f"Category='{CATEGORY_FILTER}'")
    scope_str = " + ".join(scope_bits)
    print(f"\n  Keep-list: {len(keep)} SKU(s) read from {os.path.basename(PRICE_FILE_PATH)}")
    print(f"  Scope:     {scope_str}")

    # --- In-brand product set. Use a recent index (rebuild only if older than 2h);
    # the live GET on each target re-checks its status at write time, so this stays
    # safe while avoiding a full ~3-min rebuild on every back-to-back run. ---
    print("\nFinding products in scope...")
    try:
        index, cat_generated = load_catalogue_index(
            refresh=REFRESH_CATALOGUE, max_age_hours=min(CATALOGUE_MAX_AGE_HOURS, 2))
    except Exception as e:
        print(f"\nERROR building Cin7 catalogue index: {e}")
        return
    matched, brand_rows, cats_for_brand = filter_catalogue(
        index, brand=BRAND_FILTER, category=CATEGORY_FILTER,
        exclude_bathroom_brands=EXCLUDE_BATHROOM_BRANDS)
    if not matched:
        print(f"\nNo products found for {scope_str}. Nothing to do.")
        if brand_rows == 0:
            print(f"  (Nothing matched Brand='{BRAND_FILTER}' at all — check the brand name.)")
        return

    candidates = [(sku, name) for (sku, name) in matched if sku.upper() not in keep]
    if not candidates:
        print(f"\nAll {len(matched)} product(s) in {scope_str} are present in the file.")
        print("Nothing to deprecate.")
        return

    # --- Stock check: pull availability once; hold anything still in stock. ---
    print(f"\n{len(candidates)} in-scope product(s) are not in the file — checking stock...")
    try:
        avail = cin7_fetch_availability_map()
    except Exception as e:
        print(f"\nERROR fetching stock availability: {e}")
        print("Refusing to deprecate without a stock check.")
        return

    to_deprecate = []   # (sku, name, onhand, onorder)
    held         = []   # (sku, name, onhand, onorder, reason)
    for (sku, name) in candidates:
        slot   = avail.get(sku.upper(), {})
        onhand = slot.get("OnHand", 0.0)
        onord  = slot.get("OnOrder", 0.0)
        if onhand > 0:
            held.append((sku, name, onhand, onord, f"{onhand:g} on hand"))
        elif DEPRECATE_HOLD_ON_ORDER and onord > 0:
            held.append((sku, name, onhand, onord, f"{onord:g} on order"))
        else:
            to_deprecate.append((sku, name, onhand, onord))

    # --- Pre-flight summary with the explicit lists ---
    print("\n" + "=" * 60)
    print("  PRE-FLIGHT SUMMARY — DEPRECATION")
    print("=" * 60)
    print(f"  Scope:         {scope_str}")
    if EXCLUDE_BATHROOM_BRANDS:
        print(f"                 (Bathroom Brands excluded)")
    print(f"  In file:       {len(keep)} SKU(s) — KEPT")
    print(f"  In brand:      {len(matched)} product(s) in Cin7")
    print(f"  Not in file:   {len(candidates)} candidate(s)")
    print(f"  Catalogue:     {len(index)} products indexed (fresh, built {cat_generated})")
    print(f"  New status:    Active -> '{DEPRECATE_STATUS}'")
    print(f"  Stock rule:    hold if on hand > 0"
          + (" or on order > 0" if DEPRECATE_HOLD_ON_ORDER else "")
          + " (sells through, deprecates on a later run)")
    print("-" * 60)
    print(f"  HELD — still in stock, NOT deprecated: {len(held)}")
    for (sku, name, oh, oo, reason) in held[:DEPRECATE_DISPLAY_LIMIT]:
        print(f"     keep   {sku:<20} {reason:<14} {name[:38]}")
    if len(held) > DEPRECATE_DISPLAY_LIMIT:
        print(f"     ...and {len(held) - DEPRECATE_DISPLAY_LIMIT} more (see log)")
    print("-" * 60)
    print(f"  WILL DEPRECATE — no stock: {len(to_deprecate)}")
    for (sku, name, oh, oo) in to_deprecate[:DEPRECATE_DISPLAY_LIMIT]:
        tail = f"  (on order {oo:g})" if oo else ""
        print(f"     DEPR   {sku:<20} {name[:44]}{tail}")
    if len(to_deprecate) > DEPRECATE_DISPLAY_LIMIT:
        print(f"     ...and {len(to_deprecate) - DEPRECATE_DISPLAY_LIMIT} more (full list in the log)")
    print("=" * 60)

    # Held rows are logged in both dry and live (they document the decision).
    results = [_depr_row(sku, name, "held", onhand=oh, onorder=oo,
                         note=reason, success=True)
               for (sku, name, oh, oo, reason) in held]

    if DRY_RUN:
        for (sku, name, oh, oo) in to_deprecate:
            results.append(_depr_row(sku, name, "would_deprecate", onhand=oh, onorder=oo,
                                     new_status=DEPRECATE_STATUS, success=True))
        _write_deprecate_log(LOG_FILE_PATH, results)
        print(f"\nDRY RUN — no changes made. {len(to_deprecate)} would be deprecated, "
              f"{len(held)} held in stock.")
        print(f"Log saved to: {LOG_FILE_PATH}")
        print("\nReview the 'WILL DEPRECATE' list above. If it's correct, set "
              "DRY_RUN: False and re-run.")
        print("Tip: trim the file to a single in-stock-zero line first to prove one "
              "deprecate end-to-end before the full batch.")
        return

    # --- Live gates: business hours -> Zap off -> typed CONFIRM ---
    if not check_business_hours():
        return
    if not confirm_zap_paused():
        print("  Cancelled — turn the Zap off and re-run. No changes made.")
        return
    print(f"\n  About to DEPRECATE {len(to_deprecate)} product(s) in {scope_str} LIVE.")
    print(f"  {len(held)} in-stock product(s) will be left active.")
    print(f"  This sets their Cin7 status to '{DEPRECATE_STATUS}'. An undo file will be written.")
    print("  Type CONFIRM to proceed, or press Enter to cancel:")
    if not confirm_typed():
        print("  Cancelled. No changes made.")
        return
    print()

    undo_rows = []
    done = errors = skipped_already = 0
    i = 0
    while i < len(to_deprecate):
        sku, name, oh, oo = to_deprecate[i]
        print(f"[{i+1}/{len(to_deprecate)}] Deprecating: {sku}")
        try:
            prod = cin7_get_product_by_sku(sku)
            if not prod:
                results.append(_depr_row(sku, name, "error", onhand=oh, onorder=oo,
                                         error="SKU not found at write time"))
                errors += 1; i += 1; continue
            pid   = prod.get("ID") or prod.get("Id") or prod.get("ProductID")
            prior = clean(prod.get("Status", "")).strip() or "Active"
            if prior.lower() == DEPRECATE_STATUS.lower():
                results.append(_depr_row(sku, name, "already_deprecated", onhand=oh,
                                         onorder=oo, prior_status=prior,
                                         new_status=prior, success=True))
                skipped_already += 1
                print(f"  [skip] {sku}: already '{prior}'")
                i += 1; continue
            if not pid:
                results.append(_depr_row(sku, name, "error", onhand=oh, onorder=oo,
                                         prior_status=prior, error="No product ID returned"))
                errors += 1; i += 1; continue
            ok, err = cin7_set_product_status(pid, DEPRECATE_STATUS)
            if ok:
                results.append(_depr_row(sku, name, "deprecated", onhand=oh, onorder=oo,
                                         prior_status=prior, new_status=DEPRECATE_STATUS,
                                         success=True))
                undo_rows.append({"SKU": sku, "Name": name, "PriorStatus": prior,
                                  "ProductID": pid,
                                  "DeprecatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                done += 1
                print(f"  [OK] {sku}: {prior} -> {DEPRECATE_STATUS}")
            else:
                results.append(_depr_row(sku, name, "error", onhand=oh, onorder=oo,
                                         prior_status=prior, error=err))
                errors += 1
                print(f"  [ERROR] {sku}: {err}")
            i += 1
        except KeyboardInterrupt:
            print("\n  Ctrl+C — stopping. Saving log + undo for what's done so far.")
            break
        except Exception as e:
            results.append(_depr_row(sku, name, "error", onhand=oh, onorder=oo, error=str(e)))
            errors += 1; i += 1
            print(f"  [ERROR] {sku}: {e}")

    _write_deprecate_log(LOG_FILE_PATH, results)
    if undo_rows:
        _write_undo_file(undo_path, undo_rows)

    print("\n" + "=" * 60)
    print(f"Deprecation complete. {done} deprecated, {len(held)} held in stock"
          + (f", {skipped_already} already deprecated" if skipped_already else "")
          + (f", {errors} error(s)" if errors else "") + ".")
    print(f"Log saved to:  {LOG_FILE_PATH}")
    if undo_rows:
        print(f"Undo file:     {undo_path}")
        print(f"\nTo reverse this run (set those {len(undo_rows)} product(s) back to their")
        print("previous status), run:")
        print(f'    cd "{SCRIPT_DIR}"')
        print(f"    python {os.path.basename(__file__)} --reactivate last")
    if errors:
        print(f"\n{errors} error(s) — check the log. Re-running deprecate mode will retry")
        print("them (already-deprecated SKUs are skipped automatically).")
    print("=" * 60)


def find_latest_undo():
    logs_dir = os.path.join(SCRIPT_DIR, "Logs")
    try:
        files = [os.path.join(logs_dir, f) for f in os.listdir(logs_dir)
                 if f.startswith("deprecate_undo_") and f.endswith(".csv")]
    except FileNotFoundError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def _parse_reactivate_arg():
    """Return the undo-file argument if --reactivate/--undo was passed, else None.
    'last'/'latest'/absent -> newest deprecate_undo_*.csv."""
    args = sys.argv[1:]
    if args and args[0].lower() in ("--reactivate", "--undo"):
        return args[1] if len(args) > 1 else "last"
    return None


def _parse_audit_arg():
    """True if the first CLI argument is --audit (read-only tier check)."""
    args = sys.argv[1:]
    return bool(args) and args[0].lower() == "--audit"


def run_reactivate_mode(undo_arg):
    """Undo a deprecation run: read its undo CSV and set each SKU back to its prior
    status (usually Active). Honours DRY_RUN and the same live gates."""
    global LOG_FILE_PATH
    if undo_arg in (None, "last", "latest"):
        undo_file = find_latest_undo()
        if not undo_file:
            print("REACTIVATE: no deprecate_undo_*.csv found in Logs — nothing to undo.")
            return
    else:
        undo_file = undo_arg
        if not os.path.isabs(undo_file) and not os.path.exists(undo_file):
            cand = os.path.join(SCRIPT_DIR, "Logs", undo_file)
            if os.path.exists(cand):
                undo_file = cand
    if not os.path.exists(undo_file):
        print(f"REACTIVATE: undo file not found: {undo_file}")
        return

    targets = []
    with open(undo_file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sku = (row.get("SKU") or "").strip()
            if sku:
                prior = (row.get("PriorStatus") or "Active").strip() or "Active"
                targets.append((sku, (row.get("Name") or "").strip(), prior))
    if not targets:
        print(f"REACTIVATE: no SKUs in {os.path.basename(undo_file)} — nothing to do.")
        return

    LOG_FILE_PATH = os.path.join(
        SCRIPT_DIR, "Logs",
        f"reactivate_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

    print("=" * 60)
    print("RHS Group Ltd — Reactivate (undo deprecation)")
    print(f"Mode:       {'DRY RUN (no changes will be made)' if DRY_RUN else '*** LIVE — products will be REACTIVATED ***'}")
    print(f"Undo file:  {undo_file}")
    print(f"Log file:   {LOG_FILE_PATH}")
    print("=" * 60)
    print(f"\n  {len(targets)} product(s) will be set back to their previous status")
    print(f"  (from '{DEPRECATE_STATUS}', usually back to 'Active').")
    for (sku, name, prior) in targets[:DEPRECATE_DISPLAY_LIMIT]:
        print(f"     {sku:<20} -> {prior:<10} {name[:38]}")
    if len(targets) > DEPRECATE_DISPLAY_LIMIT:
        print(f"     ...and {len(targets) - DEPRECATE_DISPLAY_LIMIT} more")

    results = []
    if DRY_RUN:
        for (sku, name, prior) in targets:
            results.append(_depr_row(sku, name, "would_reactivate",
                                     new_status=prior, success=True))
        _write_deprecate_log(LOG_FILE_PATH, results)
        print(f"\nDRY RUN — no changes. {len(targets)} would be reactivated.")
        print(f"Log saved to: {LOG_FILE_PATH}")
        return

    if not check_business_hours():
        return
    if not confirm_zap_paused():
        print("  Cancelled — turn the Zap off and re-run. No changes made.")
        return
    if not confirm_proceed(f"Reactivate {len(targets)} product(s) LIVE?"):
        print("  Cancelled. No changes made.")
        return
    print()

    done = errors = 0
    i = 0
    while i < len(targets):
        sku, name, prior = targets[i]
        print(f"[{i+1}/{len(targets)}] Reactivating: {sku}")
        try:
            prod = cin7_get_product_by_sku(sku)
            if not prod:
                results.append(_depr_row(sku, name, "error", new_status=prior,
                                         error="SKU not found"))
                errors += 1; i += 1; continue
            pid = prod.get("ID") or prod.get("Id") or prod.get("ProductID")
            cur = clean(prod.get("Status", "")).strip()
            if not pid:
                results.append(_depr_row(sku, name, "error", error="No product ID returned"))
                errors += 1; i += 1; continue
            ok, err = cin7_set_product_status(pid, prior)
            if ok:
                results.append(_depr_row(sku, name, "reactivated", prior_status=cur,
                                         new_status=prior, success=True))
                done += 1
                print(f"  [OK] {sku}: {cur or '?'} -> {prior}")
            else:
                results.append(_depr_row(sku, name, "error", prior_status=cur,
                                         new_status=prior, error=err))
                errors += 1
                print(f"  [ERROR] {sku}: {err}")
            i += 1
        except KeyboardInterrupt:
            print("\n  Ctrl+C — stopping. Saving log.")
            break
        except Exception as e:
            results.append(_depr_row(sku, name, "error", error=str(e)))
            errors += 1; i += 1
            print(f"  [ERROR] {sku}: {e}")

    _write_deprecate_log(LOG_FILE_PATH, results)
    print("\n" + "=" * 60)
    print(f"Reactivation complete. {done} reactivated"
          + (f", {errors} error(s)" if errors else "") + ".")
    print(f"Log saved to: {LOG_FILE_PATH}")
    print("=" * 60)


def main():
    global LOG_FILE_PATH

    # --- Audit (read-only tier check)? Via the --audit switch or AUDIT_MODE in Config. ---
    if _parse_audit_arg() or AUDIT_MODE:
        run_audit_mode()
        return

    # --- Reactivate (undo) mode? Highest-priority command-line switch. ---
    reactivate_arg = _parse_reactivate_arg()
    if reactivate_arg is not None:
        run_reactivate_mode(reactivate_arg)
        return

    # --- Deprecate mode? Config-driven; its own self-contained flow. ---
    if DEPRECATE_MODE:
        run_deprecate_mode()
        return

    uplift_ctx = None
    rows = []

    # --- Retry mode? Resolve the source log and its failed SKUs up front ---
    retry_mode, retry_log = _parse_retry_arg()
    error_skus = None
    if retry_mode:
        if retry_log in (None, "last", "latest"):
            retry_log = find_latest_log()
            if not retry_log:
                print("RETRY MODE: no logs found in the Logs folder — nothing to retry.")
                return
        elif not os.path.isabs(retry_log) and not os.path.exists(retry_log):
            candidate = os.path.join(SCRIPT_DIR, "Logs", retry_log)
            if os.path.exists(candidate):
                retry_log = candidate
        if not os.path.exists(retry_log):
            print(f"RETRY MODE: log file not found: {retry_log}")
            return
        failed = load_error_skus_from_log(retry_log)
        if not failed:
            print(f"RETRY MODE: no failed SKUs in {os.path.basename(retry_log)} — nothing to retry.")
            return
        error_skus = set(failed)
        print(f"RETRY MODE — reading: {os.path.basename(retry_log)} "
              f"({len(error_skus):,} failed SKU(s) to retry)")
        LOG_FILE_PATH = os.path.join(
            SCRIPT_DIR, "Logs",
            f"price_update_retry_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

    if UPLIFT_MODE:
        # ---- Manufacturer uplift mode: no price file, enumerate by filter ----
        if not BRAND_FILTER and not CATEGORY_FILTER:
            print("\nERROR: UPLIFT_MODE is on but both BRAND_FILTER and CATEGORY_FILTER are blank.")
            print("Refusing to uplift the entire catalogue. Set a brand and/or category in Config.txt.")
            return
        if UPLIFT_PERCENT <= 0:
            print(f"\nERROR: UPLIFT_MODE is on but UPLIFT_PERCENT is {UPLIFT_PERCENT}.")
            print("Set a positive percentage in Config.txt (e.g. UPLIFT_PERCENT: 5).")
            return

        scope_bits = []
        if BRAND_FILTER:
            scope_bits.append(f"Brand='{BRAND_FILTER}'")
        if CATEGORY_FILTER:
            scope_bits.append(f"Category='{CATEGORY_FILTER}'")
        scope_str    = " + ".join(scope_bits)
        pct_str      = format_decimal_clean(UPLIFT_PERCENT)
        factor_str   = format_decimal_clean(Decimal("1") + UPLIFT_PERCENT / Decimal("100"))
        uplift_ctx   = {
            "percent":       UPLIFT_PERCENT,
            "supplier_cost": UPLIFT_SUPPLIER_COST,
            "label":         f"Manufacturer uplift +{pct_str}% - {scope_str}",
        }

        print("=" * 60)
        print("RHS Group Ltd — Manufacturer Price Uplift")
        print(f"Mode:       {'DRY RUN (no changes will be made)' if DRY_RUN else '*** LIVE — changes will be applied ***'}")
        print(f"Log file:   {LOG_FILE_PATH}")
        print("=" * 60)
        print(f"\nFinding products matching {scope_str} ...")
        try:
            index, cat_generated = load_catalogue_index(
                refresh=REFRESH_CATALOGUE, max_age_hours=CATALOGUE_MAX_AGE_HOURS)
        except Exception as e:
            print(f"\nERROR building Cin7 catalogue index: {e}")
            return

        matched, brand_rows, cats_for_brand = filter_catalogue(
            index, brand=BRAND_FILTER, category=CATEGORY_FILTER,
            exclude_bathroom_brands=EXCLUDE_BATHROOM_BRANDS)
        rows = [(sku, name, Decimal("0"), {}, {}) for (sku, name) in matched]

        # Retry mode: keep only the SKUs that failed last time (before the
        # pre-flight summary, so the count and CONFIRM reflect the real retry set)
        if retry_mode:
            rows = _apply_retry_filter(rows, error_skus)
            if not rows:
                print("Nothing to retry from this uplift scope. Exiting.")
                return

        if not rows:
            # If the brand matched but the category knocked it to zero, show the
            # categories that actually exist for that brand so the filter can be fixed.
            if BRAND_FILTER and CATEGORY_FILTER and brand_rows > 0:
                print(f"\n  {brand_rows} product(s) match Brand='{BRAND_FILTER}', "
                      f"but none also have Category='{CATEGORY_FILTER}'.")
                print(f"  Categories that exist for that brand:")
                for cat, n in sorted(cats_for_brand.items(), key=lambda kv: -kv[1]):
                    print(f"     {n:>5}  {cat or '(blank)'}")
                print(f"\n  Set CATEGORY_FILTER to one of the above exactly, "
                      f"or leave it blank to uplift the whole brand.")
            else:
                print("No products matched the filter. Nothing to uplift. Exiting.")
            return

        # On a LIVE run, check business hours BEFORE the ~7-min price scan so we
        # fail fast rather than make the operator wait then abort.
        if not DRY_RUN:
            if not check_business_hours():
                return

        # ---- Uplift double-run guard: skip products that already look uplifted ----
        skip_skus = set()
        if UPLIFT_GUARD:
            default_supplier = UPLIFT_DEFAULT_SUPPLIER.strip().lower()
            print("\n  Checking current prices for products that may already be uplifted")
            print("  (fetching live prices — a full catalogue scan, ~7 min; "
                  "set UPLIFT_GUARD: False to skip)")
            try:
                pricing = cin7_fetch_pricing_for_skus([r[0] for r in rows])
            except Exception as e:
                print(f"\n  WARNING: price check failed ({e}).")
                print("  Continuing WITHOUT the already-uplifted guard.")
                pricing = None

            if pricing is not None:
                skipped, unchecked = classify_uplift_targets(rows, pricing, default_supplier)
                skip_skus = {s[0] for s in skipped}

                basis_desc = (f"supplier '{UPLIFT_DEFAULT_SUPPLIER}' (or each product's "
                              f"dearest where it isn't attached)"
                              if default_supplier else
                              "each product's highest-priced supplier")
                print("\n" + "-" * 60)
                print("  ALREADY-UPLIFTED CHECK")
                print("-" * 60)
                print(f"  Basis:          {basis_desc}")
                print(f"  Will uplift:    {len(rows) - len(skip_skus)}")
                print(f"  Skip (already above basis): {len(skip_skus)}")
                if unchecked:
                    print(f"  Couldn't check: {len(unchecked)} (no cost / no current "
                          f"price — left in, will uplift if priced)")

                if skipped:
                    print(f"\n  Skipping these — Tier10 already above the supplier basis:")
                    SHOW_CAP = 50
                    for sku, name, expected, actual in skipped[:SHOW_CAP]:
                        print(f"     {sku:<18} basis {format_decimal_clean(expected):>9}"
                              f"  now {format_decimal_clean(actual):>9}  {name[:36]}")
                    if len(skipped) > SHOW_CAP:
                        print(f"     ... and {len(skipped) - SHOW_CAP} more.")

                    rows = [r for r in rows if r[0] not in skip_skus]
                    if not rows:
                        print("\n  Every matched product is already uplifted. "
                              "Nothing to do.")
                        return
                else:
                    print("\n  None look already-uplifted — all will be uplifted.")

        # ---- Pre-flight summary: exactly what this run will do ----
        print("\n" + "=" * 60)
        print("  PRE-FLIGHT SUMMARY")
        print("=" * 60)
        print(f"  Action:        Increase prices by +{pct_str}%")
        print(f"  Scope:         {scope_str}")
        if EXCLUDE_BATHROOM_BRANDS:
            print(f"                 (Bathroom Brands excluded)")
        print(f"  Products:      {len(rows)} will be updated")
        if UPLIFT_GUARD and skip_skus:
            print(f"  Guard:         {len(skip_skus)} already-uplifted — SKIPPED")
        elif UPLIFT_GUARD:
            print(f"  Guard:         on — none skipped")
        print(f"  Catalogue:     {len(index)} products indexed (built {cat_generated})")
        print(f"  Supplier cost: {'UPLIFTED by the same %' if UPLIFT_SUPPLIER_COST else 'HELD unchanged (waiting for real price file)'}")
        print(f"  Selling tiers: recomputed from each Tier10 anchor x {factor_str}")
        print(f"  Targets:       Cin7 [{'Y' if UPDATE_CIN7 else 'n'}]  "
              f"simPRO [{'Y' if UPDATE_SIMPRO else 'n'}]  Shopify [{'Y' if UPDATE_SHOPIFY else 'n'}]")
        print(f"  Mode:          {'DRY RUN — preview only, no changes written' if DRY_RUN else 'LIVE — changes will be written'}")
        print("=" * 60)

        # Live runs: confirm the Zap is paused, then an explicit typed confirmation.
        # (Business hours were already checked before the price scan above.)
        if not DRY_RUN:
            if not confirm_zap_paused():
                print("  Cancelled — turn the Zap off and re-run. No changes made.")
                return
            print(f"\n  About to apply +{pct_str}% to {len(rows)} products LIVE.")
            print("  Type CONFIRM to proceed, or press Enter to cancel:")
            if not confirm_typed():
                print("  Cancelled. No changes made.")
                return
            print()
    else:
        # ---- File mode: cost-driven from a CSV (unchanged behaviour) ----
        print("=" * 60)
        print("RHS Group Ltd — Supplier Price Update")
        print(f"Mode:       {'DRY RUN (no changes will be made)' if DRY_RUN else '*** LIVE — changes will be applied ***'}")
        print(f"Price file: {PRICE_FILE_PATH}")
        print(f"Log file:   {LOG_FILE_PATH}")
        print("=" * 60)

        # Read price file
        try:
            with open(PRICE_FILE_PATH, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                # Resolve which extra attribute columns are present (case-insensitive)
                header_lookup = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
                attr_cols = {}  # actual CSV header -> Cin7 field
                for col, cin7_field in ATTRIBUTE_COLUMN_MAP.items():
                    h = header_lookup.get(col.strip().lower())
                    if h:
                        attr_cols[h] = cin7_field
                # Resolve create-only columns (used when CREATE_MISSING adds a product)
                create_cols = {}  # logical name -> actual CSV header
                for cname in ("Category", "Brand", "CostingMethod", "Discount", "Supplier"):
                    h = header_lookup.get(cname.lower())
                    if h:
                        create_cols[cname] = h
                for row in reader:
                    sku  = row.get("SKU",  "").strip()
                    name = row.get("Name", "").strip()
                    cost = row.get("Cost", "").strip()
                    if sku and cost:
                        attrs = {}
                        for h, cin7_field in attr_cols.items():
                            v = (row.get(h) or "").strip()
                            if v:
                                attrs[cin7_field] = v
                        cfields = {}
                        for cname, h in create_cols.items():
                            v = (row.get(h) or "").strip()
                            if v:
                                cfields[cname] = v
                        rows.append((sku, name, to_decimal_safe(cost), attrs, cfields))
        except FileNotFoundError:
            print(f"\nERROR: Price file not found at '{PRICE_FILE_PATH}'")
            print("Check the PRICE_FILE_PATH setting at the top of the script.")
            return

        if not rows:
            print("No valid rows found in price file. Exiting.")
            return

        if attr_cols:
            print("Attribute columns detected: "
                  + ", ".join(f"{h} -> {f}" for h, f in attr_cols.items())
                  + f"  (mode: {ATTRIBUTE_FILL_MODE})")
        if CREATE_MISSING:
            cc = ", ".join(create_cols.keys()) if create_cols else "none (using Config defaults)"
            print(f"Create-missing: ON — SKUs not found in Cin7 will be CREATED. "
                  f"Create columns: {cc}")

        # Retry mode: keep only the SKUs that failed last time
        if retry_mode:
            rows = _apply_retry_filter(rows, error_skus)
            if not rows:
                print("Nothing to retry from this price file. Exiting.")
                return

        # ---- Pre-flight summary: exactly what this run will do ----
        attr_note = (", ".join(sorted(set(attr_cols.values()))) if attr_cols else "")

        # When creating, classify file SKUs against the cached catalogue index for
        # an up-front update/create split — read straight from the cache file, so
        # it never triggers a full rebuild and costs no API calls.
        create_estimate = None
        if CREATE_MISSING:
            if os.path.exists(CATALOGUE_CACHE_PATH):
                try:
                    with open(CATALOGUE_CACHE_PATH, "r", encoding="utf-8") as cf:
                        cdata = json.load(cf)
                    known = {clean(p.get("SKU", "")).strip() for p in cdata.get("products", [])}
                    miss  = sum(1 for r in rows if r[0] not in known)
                    create_estimate = (miss, len(rows) - miss, cdata.get("generated", "?"))
                except Exception:
                    create_estimate = "unreadable"
            else:
                create_estimate = "no-cache"

        print("\n" + "=" * 60)
        print("  PRE-FLIGHT SUMMARY")
        print("=" * 60)
        print(f"  Source:        {os.path.basename(PRICE_FILE_PATH)}")
        print(f"  SKUs:          {len(rows)} to process")
        if isinstance(create_estimate, tuple):
            c_new, c_upd, built = create_estimate
            print(f"  Update/Create: ~{c_upd} existing -> update, ~{c_new} not found -> CREATE")
            print(f"                 (estimate from catalogue index built {built}; the live GET decides per SKU)")
        elif create_estimate == "no-cache":
            print(f"  Update/Create: split unknown (no catalogue cache yet — the live GET decides per SKU)")
        print(f"  Update:        prices + supplier cost"
              + (f" + attributes ({attr_note}, {ATTRIBUTE_FILL_MODE})" if attr_cols else ""))
        if CREATE_MISSING:
            print(f"  Create new:    ON — type {NEW_PRODUCT_TYPE}, {NEW_PRODUCT_COSTING_METHOD}, "
                  f"UOM {NEW_PRODUCT_UOM}, location {NEW_PRODUCT_LOCATION}")
        print(f"  Targets:       Cin7 [{'Y' if UPDATE_CIN7 else 'n'}]  "
              f"simPRO [{'Y' if UPDATE_SIMPRO else 'n'}]  Shopify [{'Y' if UPDATE_SHOPIFY else 'n'}]")
        if retry_mode:
            print(f"  Retry:         only the {len(rows)} SKU(s) that failed last run")
        print(f"  Mode:          {'DRY RUN — preview only, no changes written' if DRY_RUN else 'LIVE — changes WILL be written'}")
        print("=" * 60)

        # Live runs: business-hours guard, then an explicit [Y/N] confirmation
        if not DRY_RUN:
            if not check_business_hours():
                return
            if not confirm_zap_paused():
                print("  Cancelled — turn the Zap off and re-run. No changes made.")
                return
            if not confirm_proceed(f"Apply these changes to {len(rows)} SKU(s) LIVE?"):
                print("  Cancelled. No changes made.")
                return
        print()

    # Initialise simPRO token
    access_token = [SIMPRO_ACCESS_TOKEN]
    if not DRY_RUN:
        if not simpro_validate_token(access_token[0]):
            print("Refreshing simPRO access token...")
            access_token[0] = simpro_refresh_token()
            print("Token refreshed OK.\n")

    # Look up simPRO vendor once — same vendor for every SKU
    vendor_info = None
    if not DRY_RUN and UPDATE_SIMPRO:
        print("Looking up simPRO vendor...")
        vendor_lookup = simpro_find_vendor(access_token[0], SIMPRO_SUPPLIER_NAME)
        if not vendor_lookup.get("ok"):
            print(f"ERROR: Could not find simPRO vendor '{SIMPRO_SUPPLIER_NAME}': {vendor_lookup.get('error')}")
            return
        vendor_info = vendor_lookup["vendor"]
        print(f"Vendor found: {vendor_info.get('Name')} (ID {vendor_info.get('ID')})\n")

    # Process each SKU
    results = []
    stopped_early = False
    i = 0
    while i < len(rows):
        sku, file_name, new_cost, file_attrs, create_fields = rows[i]
        print(f"[{i+1}/{len(rows)}] Processing SKU: {sku}")
        try:
            results.append(process_sku(sku, file_name, new_cost, access_token,
                                       vendor_info, uplift=uplift_ctx,
                                       file_attrs=file_attrs, create_fields=create_fields))
            i += 1
        except KeyboardInterrupt:
            print(f"\n\n  Ctrl+C — interrupted during SKU {sku}.")
            print("  Press Enter to retry this SKU, or Q + Enter to stop:")
            try:
                resp = input("  > ").strip().upper()
            except KeyboardInterrupt:
                resp = "Q"
            if resp == "Q":
                results.append({
                    "SKU": sku, "FileName": file_name, "Action": "interrupted",
                    "NewSupplierCost": float(new_cost), "OldSupplierCost": "", "Name": "",
                    "Tier1": "", "Tier2": "", "Tier3": "", "Tier4": "", "Tier5": "",
                    "Tier6": "", "Tier7": "", "Tier8": "", "Tier9": "", "Tier10": "",
                    "SimproNewPrice": "", "AttributeChanges": "", "Cin7Updated": False,
                    "SimproUpdated": False,
                    "ShopifyUpdated": False, "ShopifyPrice": "", "ShopifyCompareAtPrice": "",
                    "DryRun": DRY_RUN, "Success": False, "Error": "Interrupted by user (Ctrl+C)"
                })
                print("  Stopping early — saving log for completed SKUs.")
                stopped_early = True
                break
            print(f"  Retrying SKU {sku}...\n")

    # Write log CSV
    log_fields = [
        "SKU", "FileName", "Action", "Name", "OldSupplierCost", "NewSupplierCost",
        "Tier1", "Tier2", "Tier3", "Tier4", "Tier5",
        "Tier6", "Tier7", "Tier8", "Tier9", "Tier10",
        "SimproNewPrice", "AttributeChanges", "Cin7Updated", "SimproUpdated",
        "ShopifyUpdated", "ShopifyPrice", "ShopifyCompareAtPrice",
        "DryRun", "Success", "Error"
    ]
    with open(LOG_FILE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=log_fields)
        writer.writeheader()
        writer.writerows(results)

    # Print summary
    success_count = sum(1 for r in results if r["Success"])
    error_count   = len(results) - success_count
    skipped_count = len(rows) - len(results)
    created_count = sum(1 for r in results if r.get("Action") in ("created", "would_create") and r["Success"])
    updated_count = sum(1 for r in results if r.get("Action") in ("updated", "would_update") and r["Success"])
    print("\n" + "=" * 60)
    if stopped_early:
        print(f"Stopped early. {success_count}/{len(results)} SKUs processed successfully"
              + (f" ({skipped_count} not reached)." if skipped_count else "."))
    else:
        print(f"Complete. {success_count}/{len(results)} SKUs processed successfully.")
    if CREATE_MISSING or created_count:
        verb = "Would create / update" if DRY_RUN else "Created / updated"
        print(f"  {verb}: {created_count} new, {updated_count} existing")
    if error_count:
        print(f"  {error_count} error(s) — check the log file for details.")
    print(f"Log saved to: {LOG_FILE_PATH}")
    print("=" * 60)
    if DRY_RUN:
        print("\nThis was a DRY RUN. Set DRY_RUN = False to apply live changes.")

    # Point the way to retrying any failures from this run
    if error_count:
        script_name = os.path.basename(__file__)
        if retry_mode:
            print(f"\n{error_count} SKU(s) still failed. To retry them again, run:")
        else:
            print(f"\nTo retry just the {error_count} failed SKU(s), run:")
        print(f'    cd "{SCRIPT_DIR}"')
        print(f"    python {script_name} --retry last")


if __name__ == "__main__":
    main()
