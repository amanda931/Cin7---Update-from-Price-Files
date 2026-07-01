# Targeted Repricing, Creation & Deprecation Tool
### `cin7_price_updater.py`

This is the sister script to the alignment one — same plumbing, same pause switch, but a different job. Where the alignment script recalculates your **whole** catalogue from exports and only touches what's drifted, this one works on **targeted items**: it reprices them, can create ones that don't exist yet, and can retire ones that have dropped off a supplier's range.

Everything shares the same recalculation formulas, the same detailed audit notes, and the same safety rails. What changes between jobs is the input and which switch you turn on.

---

## The jobs this script does

There are four "modes" plus three command-line utilities. You pick a mode in `Config.yaml`; the utilities are triggered from the command line.

| Job | How you turn it on | What it does |
|-----|--------------------|--------------|
| **Price-file mode** (default) | `UPLIFT_MODE: False`, `DEPRECATE_MODE: False` | Reads a small CSV of new supplier costs and cascades each through Cin7, simPRO and Shopify. Can also **create** missing products. |
| **Uplift mode** | `UPLIFT_MODE: True` | No file — finds a brand/range itself and lifts its prices by a set percentage. |
| **Deprecate mode** | `DEPRECATE_MODE: True` | No price changes — reads the file as the *complete* list for one brand and retires in-brand products that have dropped off it **and hold no stock**. |
| **Tier audit** | `AUDIT_MODE: True` *or* `--audit` | Read-only. Scans the catalogue and lists products whose tiers don't match what their supplier cost should produce; writes a diagnostic CSV plus a Sheet1-format `audit_fix_*.csv` you can feed back through price-file mode. Writes nothing to Cin7/simPRO/Shopify. |
| **Audit (priced only)** (utility) | `--audit priced` on the command line | Same as the audit, but skips the unpriced (old-markup) backlog — same effect as `AUDIT_SKIP_UNPRICED: True`. |
| **Retry** (utility) | `--retry last` on the command line | Re-runs only the SKUs that failed/​errored in a previous run's log. Add `--allow-decreases` to push through cost reductions the guard held (see §1). |
| **Reactivate** (utility) | `--reactivate last` on the command line | Undoes a deprecation run — sets the retired products back to their previous status. |

These are deliberately **separate runs**. A normal price update never deprecates anything, so a partial or test file can't accidentally retire live products. Doing a full refresh from a new supplier list is two runs: one to update + create, then one to deprecate.

---

## 1. Price-file mode (the default)

### What it reads
A single CSV (default `Sheet1.csv`) with three core columns: **SKU, Name, Cost**. That's the whole input — no big inventory or supplier exports, no joining. This is the file you paste a supplier's updated prices into.

### Optional extra columns
The file can carry a few **optional columns** that are picked up automatically when present (case-insensitive headers):

- **Barcode** — written to Cin7's Barcode field (this is where Cin7 stores the GTIN/EAN; there's no separate GTIN field). Controlled by `ATTRIBUTE_FILL_MODE`:
  - `overwrite` (default) — the file always wins.
  - `fill_blank` — only set the barcode when Cin7's existing value is empty.
- **MarkUpMultiplier** — overrides the product's existing `AdditionalAttribute2` for this run's tier maths. A valid positive number on the row drives the ladder (floored at 2); a non-numeric value is ignored with a warning and the existing multiplier is used.
- **Category, Brand, CostingMethod, Discount, Supplier** — only used when a SKU has to be **created** (see below). Blank cells are ignored; nothing is overwritten with a blank.

### Cost-decrease guard (`BLOCK_COST_DECREASES`)
A backstop against a mis-keyed or mis-sorted file quietly **lowering** live prices. With `BLOCK_COST_DECREASES: True` (recommended), any SKU whose **new** file cost is below the **old** cost already in Cin7 is **held** — nothing is written for it (no cost, no tiers, no simPRO/Shopify) and it's logged with Action `skipped_cost_decrease` for review. `COST_DECREASE_TOLERANCE` (pounds) lets small rounding drops through; only a larger reduction is held. Increases and unchanged lines apply as normal. It only guards products that already have a supplier cost in Cin7 — new products and uplift mode are unaffected. After reviewing the held lines, push the genuine ones through with:

```bash
python cin7_price_updater.py --retry last --allow-decreases
```

### For each SKU
1. **Fetches the product live from Cin7** — its existing cost, multiplier (`AdditionalAttribute2`), current Tier 10 and supplier rows. So it always works against real-time data, not a snapshot.
2. **Recalculates the price ladder** off your new cost (see *How prices are calculated* below). The final Tier 10 is the **highest** of the new cost, the existing Tier 10, and the freshly proposed figure — so it only ever holds or ratchets a selling price **up**, never down.
3. **Builds a detailed audit note** for Cin7's `InternalNote`: old vs new cost, the multiplier used, the final Tier 10 and *why*, the simPRO price and catalogue ID, whether markup rules were removed, and a timestamp. The "cost rule" line records the reason (`Bulk price file update`).
4. **Pushes the changes** (live only): Cin7 (tiers + cleaned name + new supplier cost + note), then simPRO (find/create catalogue item, patch price + name, update the RHS Group Ltd vendor row), then Cin7 again (a second note once the simPRO ID is known), then Shopify (variant price + compare-at).

### Creating products that don't exist yet (`CREATE_MISSING`)
With `CREATE_MISSING: True`, a SKU in the file that **isn't found in Cin7** is **created** instead of erroring, then the normal update path layers the supplier cost, prices and note on top. A new product is priced **identically to an update** (cost × multiplier).

New products need more than a price, so the create pulls from the optional file columns first and falls back to the `NEW_PRODUCT_*` defaults in `Config.yaml` for the rest: type (Stock), costing method (FIFO), UOM, default location, the inventory/COGS/revenue account codes, the purchase/sale tax rules, the attribute set, and the multiplier.

> **Names must match Cin7 exactly.** Category, account codes, the attribute set and especially the **tax rules** must match your Cin7 reference books character-for-character, or Cin7 rejects the create with a precise validation error. The tax rules in particular include parentheses — e.g. `20% (VAT on Expenses)` / `20% (VAT on Income)`. The safe approach is to prove a **single** create first; the first one surfaces any name mismatch before a batch.

---

## 2. Manufacturer uplift mode (`UPLIFT_MODE: True`)

Use it when a manufacturer announces "all prices up 5%" and you've no tidy file to paste in.

- **No price file.** It finds the products from `BRAND_FILTER` and/or `CATEGORY_FILTER` and lifts each by `UPLIFT_PERCENT`. It **refuses to run if both filters are blank** (so you can't uplift the whole catalogue) and refuses if `UPLIFT_PERCENT` isn't positive.
- **Finding the products.** Cin7's product API ignores brand/category filters, so the script pulls the full product list once (~7 min), caches a lightweight index (SKU, name, brand, category) to `catalogue_index.json`, and filters locally. After the first build, runs are effectively instant until the cache is older than `CATALOGUE_MAX_AGE_HOURS` (default 24), then it rebuilds itself. `REFRESH_CATALOGUE: True` forces a rebuild next run. The "Bathroom Brands" grouping is skipped when `EXCLUDE_BATHROOM_BRANDS` is on, so you don't sweep the 70k+ dropship lines.
- **If your category filter finds nothing**, it lists every category that brand actually uses (with counts) so you can copy the exact one — categories are often brand-prefixed (e.g. `Carron Baths`, not a flat `Bath Panels`).
- **What it does to each price.** It lifts the existing Tier 10 by your percentage and rebuilds the ladder from that anchor — deliberately **bypassing the ratchet**, since an uplift is an intentional increase. Because every tier is a straight multiple of the anchor, all ten tiers plus the simPRO and Shopify prices move by exactly your percentage. A product with no existing Tier 10 is skipped and noted.
- **Supplier (buy) cost.** Held by default (`UPLIFT_SUPPLIER_COST: False`) — it moves your selling prices now; you update the real buy cost when the actual price file arrives. Set it `True` to raise the supplier cost by the same percentage. Either way, running the real price file later won't stack a second increase: the price-file ratchet holds the already-uplifted prices.
- **Double-run guard (`UPLIFT_GUARD`).** On by default. Before applying, the script fetches live prices and **skips** any product whose Tier 10 already sits above what its supplier cost justifies (i.e. already uplifted), so a re-run can't stack a second increase. Set `UPLIFT_GUARD: False` to skip the check. On products with more than one supplier, `UPLIFT_DEFAULT_SUPPLIER` names the supplier to measure against; blank = each product's highest-priced supplier.

---

## 3. Deprecate mode (`DEPRECATE_MODE: True`)

Retires products that have dropped off a supplier's range — but safely.

### How it decides
- The price file is read as the **complete current list for ONE brand** — the "keep" list. (Any row with a SKU is kept, even if its Cost is blank.)
- It finds every Cin7 product in `BRAND_FILTER` that **isn't** in the file. Those are candidates.
- It then checks stock: any candidate **holding stock on hand** (or, with `DEPRECATE_HOLD_ON_ORDER: True`, anything with stock **on order**) is **held back** — left active to sell through, and picked up on a later run once it's gone. Only candidates with **no stock** are set to `Deprecated`.

### Stock data
Stock comes from Cin7's `ProductAvailability` reference endpoint (`/ExternalApi/v2/ref/productavailability`), summed across all locations per SKU. That endpoint only returns rows with non-zero quantities, so any SKU absent from it genuinely holds nothing.

### The guard rails (this is the destructive one)
- **Refuses to run without `BRAND_FILTER`** — your file is one supplier's range, not the 94k catalogue, so an unscoped run would retire almost everything you sell.
- **Refuses on an empty file** — an empty keep-list would mark the whole brand for deprecation.
- **Always dry-run first.** The pre-flight prints two explicit lists — **HELD (still in stock)** and **WILL DEPRECATE (no stock)** — with counts, and writes nothing.
- **Live runs** clear the business-hours guard and the Zap-paused prompt, then require you to type **CONFIRM**.
- **An undo file is written** (`deprecate_undo_*.csv`) recording each retired SKU and its previous status.
- **Idempotent** — re-running skips anything already deprecated.

> **The WILL DEPRECATE list is your veto.** Read it before going live. The stock check protects *stocked* lines, but for brands where a lot of SKUs are made-to-order and carry zero stock (e.g. cut-to-order radiators), zero stock does **not** mean discontinued — so the **completeness of the file is the only thing protecting those live products**. Feed the genuinely complete brand list, or they'll be flagged.

### Undoing a run — reactivate
If a deprecation run was wrong, `--reactivate last` reads the newest undo file and sets every SKU back to its previous status. It honours `DRY_RUN` and the same live gates.

---

## 4. Tier audit (`AUDIT_MODE: True`, or `--audit`)

A **read-only** health check. It never writes to Cin7, simPRO or Shopify — it just tells you where your live tiers have drifted from what the supplier cost should produce.

- **What it does.** Scans the catalogue (honouring the `BRAND_FILTER` / `CATEGORY_FILTER` scope in SCOPE; blank = whole catalogue) and flags every product whose tiers don't match the recalculated ladder for its current cost.
- **What it writes.** Two files: a diagnostic CSV listing the mismatches, and a **Sheet1-format `audit_fix_*.csv`** containing just the drifted products — copy that onto `Sheet1.csv` and run price-file mode to correct them.
- **Tolerance.** A tier is only flagged when it's off by **both** `AUDIT_TOLERANCE_PERCENT` *and* `AUDIT_TOLERANCE_PENCE` at once, so penny rounding is never reported; genuine drift clears both bars.
- **Skipping the backlog.** `AUDIT_SKIP_UNPRICED: True` (or adding `priced` after `--audit`) hides the unpriced old-markup products and reports only genuinely mispriced ones.

**To apply an audit's fixes:** copy the newest `audit_fix_*.csv` onto `Sheet1.csv`, set `AUDIT_MODE: False`, dry-run, then run live. (Leave `CREATE_MISSING: False` — an audit_fix file only lists products that already exist.)

---

## Utility runs (command line)

```bash
# Normal run — mode is whatever Config.yaml selects (price-file / uplift / deprecate / audit)
python cin7_price_updater.py

# Read-only tier audit (same as AUDIT_MODE: True); add 'priced' to skip the unpriced backlog
python cin7_price_updater.py --audit
python cin7_price_updater.py --audit priced

# Retry only the SKUs that failed in the most recent log (or a named log)
python cin7_price_updater.py --retry last
python cin7_price_updater.py --retry price_update_log_20260620_112714.csv

# Retry, allowing through the cost reductions the decrease-guard held
python cin7_price_updater.py --retry last --allow-decreases

# Undo the most recent deprecation run (or a named undo file)
python cin7_price_updater.py --reactivate last
python cin7_price_updater.py --reactivate deprecate_undo_20260620_180203.csv
```

**Retry** reads a previous run's log, pulls out the SKUs that errored or were interrupted, re-runs only those against the current file/scope, and writes its own `*_retry_log_*.csv`. Failed SKUs that are no longer in the current input are reported so they aren't lost silently. **`--allow-decreases`** (alias `--allow-cost-decreases`) lifts the cost-decrease guard for that run, so genuine reductions held as `skipped_cost_decrease` are applied.

---

## How prices are calculated

The same formulas across every mode:

- **Tier 10** = new cost × multiplier ÷ 2 (multiplier floored at 2). In price-file mode the final Tier 10 is the **highest** of {new cost, existing Tier 10, proposed} — the ratchet. Uplift mode sets it directly from the uplifted anchor instead.
- **Tiers 6–9** — a margin ladder above Tier 10 at 40 / 30 / 20 / 10%.
- **Tiers 1–5** — roughly double Tier 10 with small uplifts (1 / 2 / 3 / 5 / 7.5%).
- **simPRO / Shopify price** — Tier 4 with at least a 40% discount applied; **compare-at** price = Tier 4 itself.

Product names are cleaned the same way throughout (encoding fixes, unit casing, trade acronyms).

---

## Safety rails

- **Rate limiter** keeps API calls under the per-minute cap (`RATE_LIMIT_PER_MIN`).
- **Business-hours guard** blocks live runs Mon–Fri 08:00–17:00 unless you type CONFIRM, to avoid triggering your Zapier per-product workflows en masse.
- **Zapier-paused prompt** reminds you to switch the sync Zap(s) off before a live bulk run and asks you to confirm (toggle with `ZAP_PAUSE_PROMPT`).
- **Pre-flight summary + confirmation** before every live run — a `[Y/N]` for price-file/create runs, a typed `CONFIRM` for uplift and deprecate runs.
- **Dry-run** everywhere — preview lines (and, for deprecate, the full HELD / WILL DEPRECATE lists) with nothing written.
- **Cost-decrease guard** (`BLOCK_COST_DECREASES`) — holds any file row that would lower a product's live cost/price, so a mis-keyed or mis-sorted file can't quietly drop prices; reviewed reductions go through with `--allow-decreases`.
- **Pause switch** — Ctrl+C mid-run lets you retry the current SKU or quit cleanly, with the log saved for everything done so far.

---

## Configuration reference (`Config.yaml`)

**Core**

| Key | Default | Meaning |
|-----|---------|---------|
| `DRY_RUN` | `True` | Preview only; no writes. |
| `RATE_LIMIT_PER_MIN` | `55` | API calls per minute ceiling. |
| `UPDATE_CIN7` / `UPDATE_SIMPRO` / `UPDATE_SHOPIFY` | `True`/`True`/`False` | Which systems to push to. |

**Scope & catalogue** (used by uplift and deprecate)

| Key | Default | Meaning |
|-----|---------|---------|
| `BRAND_FILTER` | *(blank)* | Brand to scope to. **Required** for deprecate mode. |
| `CATEGORY_FILTER` | *(blank)* | Optional narrower scope. |
| `EXCLUDE_BATHROOM_BRANDS` | `True` | Skip the Bathroom Brands dropship grouping. |
| `REFRESH_CATALOGUE` | `False` | Force a full catalogue rebuild next run. |
| `CATALOGUE_MAX_AGE_HOURS` | `24` | Rebuild the cached index when older than this. |

**Price-file mode**

| Key | Default | Meaning |
|-----|---------|---------|
| `ATTRIBUTE_FILL_MODE` | `overwrite` | `overwrite` or `fill_blank` for optional columns (Barcode). |
| `BLOCK_COST_DECREASES` | `True` | Hold any SKU whose new file cost is below Cin7's existing cost (writes nothing, logs `skipped_cost_decrease`). Override a reviewed run with `--allow-decreases`. |
| `COST_DECREASE_TOLERANCE` | `0.02` | Rounding slack (pounds) — a drop within this is let through; only a larger reduction is held. |

**Tier audit (`AUDIT_MODE` / `--audit`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `AUDIT_MODE` | `False` | Run the read-only tier audit instead of an update (same as `--audit`). |
| `AUDIT_SKIP_UNPRICED` | `False` | Report only genuinely mispriced products, hiding the unpriced old-markup backlog (same as `--audit priced`). |
| `AUDIT_TOLERANCE_PERCENT` | `1` | A tier must be off by at least this percent **and** the pence figure below to be flagged. |
| `AUDIT_TOLERANCE_PENCE` | `2` | …and by at least this many pence — so penny rounding is never flagged. |

**Create (`CREATE_MISSING`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `CREATE_MISSING` | `False` | Create SKUs not found in Cin7 (price-file mode only). |
| `NEW_PRODUCT_TYPE` | `Stock` | New-product type. |
| `NEW_PRODUCT_COSTING_METHOD` | `FIFO` | Costing method. |
| `NEW_PRODUCT_UOM` | `Each` | Unit of measure. |
| `NEW_PRODUCT_LOCATION` | `Warehouse` | Default location. |
| `NEW_PRODUCT_INVENTORY_ACCOUNT` / `_COGS_ACCOUNT` / `_REVENUE_ACCOUNT` | `300` / `310` / `200` | Account codes. |
| `NEW_PRODUCT_PURCHASE_TAX_RULE` / `_SALE_TAX_RULE` | `20% (VAT on Expenses)` / `20% (VAT on Income)` | Must match your Cin7 Taxation Rules reference book character-for-character (note the parentheses). |
| `NEW_PRODUCT_ATTRIBUTE_SET` | `Product Details` | Attribute set to assign. |
| `NEW_PRODUCT_MULTIPLIER` | `2` | Pricing multiplier for new products. |
| `NEW_PRODUCT_DROPSHIP` | `No Drop Ship` | Drop-ship setting applied to new products. |
| `NEW_PRODUCT_DEFAULT_CATEGORY` / `_DEFAULT_BRAND` / `_DISCOUNT` | *(blank)* | Fallbacks when the file row leaves them blank. |
| `NEW_PRODUCT_SUPPLIER` | *(blank)* | Existing Cin7 supplier to attach the buy cost to. Blank = no supplier row. |

**Uplift (`UPLIFT_MODE`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `UPLIFT_MODE` | `False` | Turn on uplift mode. |
| `UPLIFT_PERCENT` | `0` | Percentage increase (must be > 0). |
| `UPLIFT_SUPPLIER_COST` | `False` | Also raise the buy cost by the same %. |
| `UPLIFT_GUARD` | `True` | Skip products already sitting above what their cost justifies, so a re-run can't stack a second increase. |
| `UPLIFT_DEFAULT_SUPPLIER` | *(blank)* | On multi-supplier products, the supplier to measure the guard against. Blank = the highest-priced supplier. |

**Deprecate (`DEPRECATE_MODE`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `DEPRECATE_MODE` | `False` | Turn on deprecate mode. |
| `DEPRECATE_STATUS` | `Deprecated` | The Cin7 status to set. |
| `DEPRECATE_HOLD_ON_ORDER` | `True` | Also hold a line back if it has stock on order. |
| `DEPRECATE_DISPLAY_LIMIT` | `200` | How many rows to print per list in the pre-flight. |

**Zapier**

| Key | Default | Meaning |
|-----|---------|---------|
| `ZAP_PAUSE_PROMPT` | `True` | Show the "is the Zap off?" prompt before live runs. |
| `ZAP_NAME` | `Cin7 sync Zap(s)` | Label used in that prompt. |

---

## Logs

Every run writes a timestamped CSV to the `Logs` folder:

- `price_update_log_*` / `price_update_retry_log_*` — update/create runs and their retries (old vs new cost, all ten tiers, simPRO price, what updated where, errors).
- `deprecate_log_*` — the HELD / deprecated / error rows for a deprecation run.
- `deprecate_undo_*` — the undo record (SKU + previous status) for `--reactivate`.
- `reactivate_log_*` — the result of an undo run.

Each run also prints a summary of successes, errors, and (if stopped early) how many weren't reached, and points you at the exact retry/reactivate command when relevant.

---

## One caveat to remember

There's still **no "skip if correct"** — the script writes to every row it processes (every SKU in your file, or every product matched by your filter), even where nothing actually changed. On a live run that's more Zapier/API traffic than the alignment script's drift-only approach, which is why the business-hours guard and the Zap-paused prompt matter.

---

### In one line
Feed it a SKU/Name/Cost list (or point it at a brand and a percentage, or at a complete brand list to prune), and it pulls each product live from Cin7, recalculates the full pricing ladder, and pushes cost + tiers + selling prices into Cin7, simPRO and Shopify — creating or retiring products where you've asked it to — with a full audit note, safely and interruptibly.
