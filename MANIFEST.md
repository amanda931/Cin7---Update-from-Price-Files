# Cin7 — Update From Price Files — Manifest

**Tool:** `PriceUpdaterWithPauseSwitch.py` — targeted repricing, product creation, deprecation & a read-only tier audit for Cin7 Core (DEAR), with sync out to simPRO and Shopify.
**Owner:** RHS Group Ltd, trading as KPS (Knaresborough Plumbing Supplies)
**Location:** `C:\Python\Cin7 Product Updaters\Cin7 - Update from Price Files`
**Snapshot date:** 30 June 2026

> Top-level index for this tool. Deeper detail lives in the tool's own `ReadMe.md`.

---

## 1. What it does

Reads a **price file** and, for each SKU, recalculates prices from cost and writes them out:

1. **Cin7** — sets the price tiers and any supplied product attributes.
2. **simPRO** — syncs the catalogue trade price and the RHS Group vendor nett price.
3. **Shopify** — sets the variant price, compare-at price, and (new) barcode.

Price-file update is the primary workflow. The same script also has **uplift**, **deprecate**, and a read-only **tier audit** mode, selected via `Config.txt` (the audit, retry and reactivate jobs can also be triggered by command-line switch).

---

## 2. Files

| File | Purpose |
|---|---|
| `PriceUpdaterWithPauseSwitch.py` | The tool (~3,241 lines). |
| `Config.txt` | All settings — **plain `KEY: value`** text (not JSON). Mode flags, write toggles, fill mode, cost-decrease guard, new-product defaults. |
| `Credentials.txt` | Shared credentials (Cin7 / simPRO / Shopify). **Not in this folder** — git-ignored; kept locally/externally. |
| `Sheet1.csv` | The day-to-day price-file input (default `PRICE_FILE_PATH`); `Sheet1Test.csv` is a test copy. See §4. |
| `audit_fix_*.csv` | A Sheet1-format re-price file produced by an audit run; copy onto `Sheet1.csv` to apply the fixes. |
| `catalogue_index.json` | Cached lightweight catalogue (SKU/name/brand/category) for uplift & audit scoping; auto-rebuilt past `CATALOGUE_MAX_AGE_HOURS`. |
| `LowerCaseWords.txt` / `UpperCaseWords.txt` | Word-casing reference lists for the name-cleaning step. |
| `ReadMe.md` | Full tool documentation. |
| `Logs/` | Timestamped run-log CSVs (`price_update_log_*`, `*_retry_log_*`, `deprecate_log_*`, `deprecate_undo_*`, `tier_audit_*`). |
| `Helpers/`, `Debug/` | Standalone probe / speed-test / Shopify debug scripts. |

*`Logs/` and `*.csv` inputs are git-ignored (see `.gitignore`).*

---

## 3. Configuration (`Config.txt`, plain `KEY: value`)

Precedence: **command-line flag → `Config.txt` → in-script default.**

Key switches:

- **Mode:** price-file (default), `UPLIFT_MODE`, `DEPRECATE_MODE`, `AUDIT_MODE` (read-only).
- **Write targets:** `UPDATE_CIN7`, `UPDATE_SIMPRO`, `UPDATE_SHOPIFY`.
- **Safety / behaviour:** `DRY_RUN`, `REMOVE_MARKUP_PRICES`, `ATTRIBUTE_FILL_MODE` (`fill_blank` vs `overwrite`), the **ZAP pause-switch** (confirms the Cin7→simPRO sync Zap is off before a live run), and the ratchet HOLD logic (won't lower a selling price).
- **Cost-decrease guard:** `BLOCK_COST_DECREASES` (holds any SKU whose new file cost is below Cin7's existing cost, logged `skipped_cost_decrease`) with `COST_DECREASE_TOLERANCE` rounding slack; override a reviewed run with `--allow-decreases`.
- **Creation:** `CREATE_MISSING` plus `NEW_PRODUCT_*` defaults used when a file SKU isn't yet in Cin7.

**Command-line utilities:** `--audit` (read-only tier check, same as `AUDIT_MODE`; add `priced` to skip the unpriced backlog), `--retry [last|<log>]` (re-run only failed SKUs), `--reactivate [last|<undo>]` (undo a deprecation run), `--allow-decreases` (let held cost reductions through).

---

## 4. The price file

A CSV whose **column headers map to Cin7 fields**. Common columns:

```
SKU, Name, Cost, Category, Brand, Barcode, CostingMethod
```

- `SKU`, `Cost` drive the repricing; `Name` updates the product name.
- `Barcode` writes the Cin7 Barcode field (there is no separate GTIN field) and now also syncs to the Shopify variant.
- `Category`, `Brand`, `CostingMethod`, `Discount`, `Supplier` can be supplied per row.
- A per-row `MarkUpMultiplier` is honoured, falling back to `AdditionalAttribute2` (typically ×2.0; floor 2).
- Attribute writes obey `ATTRIBUTE_FILL_MODE` — `fill_blank` won't overwrite a value Cin7 already holds; `overwrite` will. Blank cells are always left as-is.

---

## 5. Pricing model

- **Tier ladder:** `(cost × 2) × (1 + uplift%)`. Tier 4 = +5% (≈ ×2.10 of cost); Tier 5 = +7.5% (≈ ×2.15) — about **+2.4%** over Tier 4.
- **simPRO / Shopify price** = `Tier4 × (1 − discount)`, where discount = `max(40%, the product's DiscountRule)`. Shopify compare-at = full Tier 4.
- Margins are worked in **gross margin** (profit on selling price), not markup.

---

## 6. External systems & API limits

| System | Role | Limit to respect |
|---|---|---|
| **Cin7 Core (DEAR) V2** | source of truth; tiers + attributes written here | ~60 calls/min — **shared** with the reporting tools, so don't run a big report and a live price sweep together |
| **simPRO** | sync target (trade price + RHS vendor nett price) | **~10 req/sec, enforced**, per build; 429 + `Retry-After`, needs backoff |
| **Shopify** | sync target (variant price, compare-at, barcode) | Admin API (REST + GraphQL); plan-dependent — confirm at point of use |

simPRO base `https://mjryder.simprosuite.com`; supplier name `RHS Group Ltd`.

---

## 7. How to run

1. Set the price-file path and the `UPDATE_*` / mode flags in `Config.txt`.
2. **Pause the Cin7→simPRO sync Zap** (the script prompts to confirm) so the script and the Zap don't double-handle.
3. **Dry-run first** (`DRY_RUN`) and review the preview / run-log.
4. Run live. Check the run-log CSV; use the retry / reactivate utilities for any failures.

---

## 8. Conventions

- **Python 3.14**, Windows, PowerShell for file ops.
- **Config-driven over command-line** — strong, consistent preference.
- **Smallest safe change**; confirm approach before modifying this script.
- **Dry-run before live**; offline tests exercise the real helpers.
- **Git commit after each change.**

---

## 9. Roadmap / open threads *(snapshot — 30 Jun 2026)*

**Recently done**
- **Cost-decrease guard** — `BLOCK_COST_DECREASES` holds any SKU whose new file cost is below Cin7's existing cost (writes nothing, logs `skipped_cost_decrease`); `COST_DECREASE_TOLERANCE` allows rounding slack; `--allow-decreases` pushes reviewed reductions through.
- **Read-only tier audit** — `--audit` / `AUDIT_MODE` scans the catalogue, lists tier drift against expected prices, and writes both a diagnostic CSV and a Sheet1-format `audit_fix_*.csv` to feed back through a price-file run.
- **Per-row `MarkUpMultiplier`** — a `MarkUpMultiplier` column overrides the product's existing `AdditionalAttribute2` for the tier maths (floor 2).
- **Barcode now syncs to Shopify** — file `Barcode` → variant, guarded by `ATTRIBUTE_FILL_MODE`, only when supplied and different, never blanks an existing one.

**Designed but NOT built** (do not assume these exist in code)
- `SIMPRO_SYNC_TIER` — decouple the simPRO sync tier from Shopify (default Tier 4) so MJ Ryder's feed can move to Tier 5 (+~2.4%) without touching web prices.
- Channel-aware `ALIGN_MODE` — full-catalogue re-sync of one channel (simPRO *or* Shopify) to its tier, with bulk-read + skip-if-unchanged + resumable checkpoint. *Currently shelved as disproportionate.*
- `HARD_RESET_PRICES` switch.

**Open questions**
- Which simPRO field is MJ Ryder's true cost (catalogue trade price vs RHS vendor nett price) — decides one-vs-two writes per record.
- How MJ Ryder is actually billed (Cin7 account tier vs synced price) — decides whether any sync change is needed at all.
