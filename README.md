# SmartRecon

SmartRecon reconciles three feeds against each other: OMS orders, bank/PSP
credits and 3PL logistics reports. The data has to pass integrity checks before
anything is matched. Every fix an analyst makes is recorded instead of being
applied to the raw files. Each order then goes through an ordered funnel of
rules, and the result can be traced back to the exact input rows.

```
 your feeds ──► 1. LOAD ──► 2. HEALTH ──► 3. FIX ──► 4. RUN ──► 5. REVIEW
 (csv/xlsx/     mapping +   blocking /    resolutions  funnel,     exception queue,
  parquet)      lineage     warning /     in the run   one step    side-by-side
                            info issues   config       at a time   source rows
                                 │            ▲
                                 └─ re-check ─┘   the gate opens only when nothing is
                                                  blocking and every warning is acknowledged
```

Two rules hold throughout:

- **Raw files are never modified.** Fixes are stored as resolutions in the run config.
- **Every run is reproducible** from three things: the input file hashes, the run config and the rule version.

## Quick start

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Build the Olist benchmark feeds (data/generated/ is not committed)
python -m recon.olistdata --source "brazil ecom" --out data/generated --seed 42

streamlit run app.py
```

`python -m recon.olistdata` keeps every source order by default. Pass `--rows N` for a smaller sample.

## The wizard

| Step | What happens |
|---|---|
| **Load** | Upload the OMS, bank and logistics files (CSV, XLSX or Parquet), or point at a **local folder** and pick a file for each feed. Uploads stay in memory and folder files are read in place, so no copies are written to disk. Local-folder mode only works when the app runs on the machine that holds the folder. The engine suggests a column mapping from column names and value patterns, and flags any suggestion it isn't sure of; nothing is mapped silently. An optional currency column (e.g. `Ccy`) is suggested when its header and values clearly match. A confirmed mapping can be saved as a profile and reused. |
| **Health** | The Health Report groups issues by severity (blocking / warning / info), with affected-row counts, sample rows and profiles. Each issue explains in plain language what was found and what happens to the affected records, and warns when it touches an implausibly large share of a file. It can be downloaded as JSON or as a standalone HTML page, both including this guidance. |
| **Fix** | Issues are split into **Needs your decision** (blocking issues, uncovered order statuses, currency mismatches) and **Sign-off only** (findings the run already flags as exceptions). Each issue has a recommended option preselected, with the reason. Sign-off issues can be acknowledged together with one note. Each choice is written into the run config and the checks run again. The run config can be downloaded. |
| **Run** | Available only once the gate is open. Runs the funnel and shows the counts at each step. |
| **Review** | The exception queue (EXCEPTION and REVIEW units plus orphan records) is ranked by amount at risk, then age. For any unit you can see its OMS, bank and 3PL rows side by side, and download the full results bundle. A separate view lists the **close watchlist**: units auto-accepted with a note (e.g. installment interest), to revisit first if the period cannot be closed. |

The **Benchmark** pages (executive overview, data contract, model diagnostics,
Olist source explorer) show the bundled Olist benchmark.

## Using it from Python

`recon.pipeline` is the only public entry point. The app and the tests both go through it.

```python
from recon.pipeline import draft_config, inspect, run, load_config

# Start from a draft config for new files...
draft = draft_config("exports/", files={"oms": "orders.xlsx", "bank": "bank.csv", "logistics": "3pl.csv"})
config = draft.config            # draft.flagged lists the mapping suggestions to confirm

# ...or load a saved run config
config = load_config("configs/olist_benchmark.json")

# sources: a folder, or the files themselves in memory as {relative path: bytes}
report = inspect("data/generated", config)   # HealthReport: issues, profiles, gate_open
outcome = run("data/generated", config)      # ReconRun, or a "blocked" run listing the open issues
outcome.results          # one row per unit: deciding step, decision, reason codes, deltas, explanation
outcome.exception_queue  # EXCEPTION + REVIEW units and orphans, ranked
outcome.close_watchlist # auto-accepted with a note; revisit first if the period won't close
outcome.settlement_batches  # one row per settlement batch (payout or remittance): its lines, credit and tie-out
outcome.bundle()         # decisions, triage queue, exception queue, orphans, close watchlist, funnel, excluded rows,
                         # settlement batches, manifest
```

### Run config

A run config is a JSON file (or YAML, if PyYAML is installed) with these parts:

- `feeds`: path, format, encoding, date format and delimiter for each feed. The feeds named `oms`, `bank` and `logistics` are the orders, bank and logistics feeds. Any other feed is a settlement report and states `"role": "settlement"`. Its mapping names the required columns `order_no`, `payout_id`, `gross` and `net`, and optionally `refund`, `settled_at` and any number of fee columns `fee_<name>` (fees and refunds may be printed as negative numbers).
- `channels`: optional. Each OMS `channel` value names its money chain. Without channels, every order is on one prepaid chain with the default pattern. When channels are declared, orders of an undeclared channel are a Health warning.
  - `prepaid`: optionally, the `order_ref_pattern` that finds its orders in a bank description (named group `ref`, e.g. `"DON (?P<ref>#\\d+)"`).
  - `marketplace`: `settlement` names the channel's settlement report. An order links to its line by the OMS `channel_order_no`, and the line's payout to the bank credit whose description (or `payout_id` column) names it. `fee_bps` is the channel's total fee on a line's gross (unset: fees are not checked). A line's gross is compared with the OMS `item_total` when mapped, else with `order_amount`. `amount_tolerance` overrides the policy's for the channel's checks.
  - `cod_remittances` (any chain): the channel's COD orders are paid by carrier remittances. An order links to its shipment by order reference, and the shipment's `remittance_id` (a logistics column) to the bank credit whose description names it. A shipment whose logistics `status` is `RETURNED` collected nothing. The COD fee is checked against the policy's `cod_fee_bps`. Without it, each COD order is paid by a credit of its own.
- `mapping`: canonical column → source column, for each feed.
- `policy`: absolute amount tolerance (default 0.01), date window for linking (default ±4 days), fee rates (`mdr_bps`, overridden per bank `payment_type` by `payment_type_fee_bps`, e.g. `{"bank_transfer": 0, "wallet": 110}`), `payer_pattern` (a regex with a named group `payer` that finds the payer in a bank description, when the bank feed has no `payer_name` column; compared with the OMS `customer_name` in linking pass 2), a `status_policy` that says which order statuses need payment and/or shipment evidence, and an optional `fx` setting (see [Currencies and FX](#currencies-and-fx)).
- `resolutions`: the fixes and acknowledgements recorded in the Fix step.
- `ground_truth`: optional. When set, precision and recall are reported for each reason code.

`configs/olist_benchmark.json` is the config for the benchmark, with every warning acknowledged.

### Mapping profiles

A mapping profile is a small JSON file. It records which column in one source system's export holds each canonical field the engine expects, for one feed. Keep one file per source system and feed.

1. On the first upload from a system, the engine suggests a mapping. You confirm or correct it on the Load page.
2. Under **Save mapping profiles**, give the source system a name and download one profile per feed.
3. On later runs, upload the profile with the new export, in the **Mapping profiles (optional)** box or through `draft_config(..., profiles=[...])`. If every column the profile names exists in the file, the mapping is reused exactly as saved, with no suggestions and nothing flagged. If any column is missing or renamed, the profile is skipped and the engine goes back to suggesting.

```json
{
  "source_system": "acme-bank",
  "feed": "bank",
  "mapping": {
    "txn_ref":       "Transaction ID",
    "description":   "Narrative",
    "credit_amount": "Credit (BRL)",
    "fee_amount":    "Charges",
    "net_amount":    "Net Credited",
    "value_date":    "Value Dt"
  }
}
```

A profile answers:

- **Which column is which?** For example, is the gross credit "Credit (BRL)" or "Net Credited"? A suggestion can pick the wrong one when two columns look alike; a profile settles it.
- **Has the export format changed?** If the bank renames "Charges" to "Fees", the profile stops matching. The engine falls back to suggesting and flags anything it's unsure of, so the change can't produce a wrong mapping without you noticing.
- **Is this run mapped the same way as the last one?** Reusing the profile keeps runs from the same system comparable.
- **Where did a mapping come from?** The source-system name shows up as the reason for each mapped column (for example "profile 'acme-bank'").

A profile stores column names only. Date format, delimiter and encoding stay in the run config's `feeds`.

## Health checks

| Severity | Checks | Fix options |
|---|---|---|
| Blocking | missing required column, values that fail type or date parsing, duplicate primary keys | remap a column, set the date format, choose a duplicate strategy (keep first / keep latest / route to review), exclude rows with a reason |
| Warning | missing or zero amounts, coverage gaps, picked up after delivered, placeholder 00:00:00 timestamps, amount outliers, order statuses not covered by the status policy, bank payment types with no fee rate (when rates per payment type are set), bank credits in another currency than their order | acknowledge with a note, set the status policy; for currency mismatches: convert using a rate source, correct the currency code, route to review, or exclude as a last resort |
| Info | amount distributions, order-status mix, currency mix per feed, txn → value-date lag percentiles | none |

Excluded rows are never dropped silently. They are reported in the run as a separate category, with their reason.

### Currencies and FX

A feed may mix currencies. Each linked order and credit are compared in their own currency. A credit whose currency differs from its order's is never compared as if it were the same currency.

- **Without FX configured** the unit is `CURRENCY_MISMATCH`.
- **With FX configured** (the "convert using a rate source" fix), the credit is converted into the order currency. The rate source is the bank's own rate column where it has a value, otherwise a rate table (CSV: `date, from, to, rate`) giving the latest rate on or before the credit's value date. A rate is order-currency units per credit-currency unit, and a table quoting only the inverse pair works too.
  - Within the amount tolerance after conversion: ties out as usual.
  - Within the FX tolerance (default 1%): `FX_DIFFERENCE`, a low-priority exception kept apart from amount errors.
  - Beyond it: `AMOUNT_MISMATCH`.
  - No rate for the date: `CURRENCY_MISMATCH`, with an explanation saying the rate is missing.

Each converted unit records the rate, its source and its date in its results and explanation, and keeps the original amounts. The manifest records the FX setting and the rate table's hash.

## The funnel

Each step only sees the units that earlier steps left undecided. Eligibility
comes before evidence. Otherwise orders with missing amounts would show up as
amount mismatches, and orders still waiting on a status policy would show up as
missing shipments.

| # | Step | Decides |
|---|---|---|
| 1 | `missing_source_evidence` | `MISSING_SOURCE_EVIDENCE`: no order amount |
| 2 | `status_policy` | `ORDER_STATUS_REQUIRES_POLICY`: a status the policy doesn't cover |
| 3 | `settle_marketplace` | every order of a marketplace channel, along its chain: `MISSING_PAYMENT` (no line, or no credit for its payout) → `AMOUNT_MISMATCH` (refund, or gross off the order) → `FEE_MISMATCH` (fees off the channel's terms) → REVIEW `SETTLEMENT_BATCH_MISMATCH` (the payout credit differs from its lines' net) → `AUTO_ACCEPT` `SETTLEMENT_TIE_OUT` |
| 4 | `settle_cod` | every COD order of a `cod_remittances` channel, along its chain: `MISSING_SHIPMENT` → `MISSING_PAYMENT` (returned, on no remittance yet, or no credit for its remittance) → `AMOUNT_MISMATCH` (COD collected off the order) → `COD_FEE_MISMATCH` → REVIEW `SETTLEMENT_BATCH_MISMATCH` (the remittance credit differs from its shipments' COD less fees) → `AUTO_ACCEPT` `REMITTANCE_TIE_OUT` |
| 5 | `link_exact` | links one-to-one on references (structured `order_ref`, or extracted from the bank description). Several credits naming the same order link as one split payment, compared as their total |
| 6 | `link_constrained` | links on same currency, amount within tolerance and date within the window, skipping payout and remittance credits. Never auto-matches when several candidates qualify, and never links a credit from a different payer than the order's customer. An order whose customer paid in several credits links to them as a split payment (`constrained_split`) when every free credit from that payer within the window together pays it, and no other open order of the customer has them in its window |
| 7 | `link_candidates` | REVIEW with `LINK_CANDIDATES`: proposes candidates with a confidence score, never auto-accepted |
| 8 | `route_review` | REVIEW: duplicate keys whose strategy is "route to review" |
| 9 | rule checks | `MISSING_PAYMENT` → `CURRENCY_MISMATCH` → `DUPLICATE_PAYMENT` (several credits, one of which alone pays the order) → `FX_DIFFERENCE` → `AMOUNT_MISMATCH` → `MISSING_SHIPMENT` → fee / COD fee → `TIMING_MISMATCH` → `POSSIBLE_INSTALLMENT_INTEREST` (REVIEW) |
| 10 | `decision` | `AUTO_ACCEPT` (`INSTALLMENT_INTEREST` when the stated interest explains the delta) |
| 11 | `reverse_pass` | `ORPHAN_PAYMENT` / `ORPHAN_SHIPMENT`: bank or 3PL records that link to no order (payout and remittance credits are reported on their settlement batch instead) |

Each unit gets one reason code (the first check it fails) and a plain-language
explanation. A problem on a marketplace order's own line, or a COD order's own shipment, is reported on that order even
when its batch is also off.
Each settlement batch gets its own result (`BATCH_TIE_OUT`, `MISSING_PAYMENT`, `SETTLEMENT_BATCH_MISMATCH` or
`PAYOUT_CREDIT_AMBIGUOUS`), with the orders of the run among its lines and those that are exceptions. It also records the step that decided it, the rule version and the
hashes of the input rows behind it.

## Benchmark results

Results on the full Olist benchmark (99,441 orders, every warning acknowledged):

| Metric | Value |
|---|---|
| Auto-accepted | 95,575 (96.1%), of which 243 with a note (installment interest) |
| Auto-accept precision | 1.000 |
| Review rate | 0.0% |
| Exceptions | 3,866 |
| Orphans | 0 |
| Close watchlist | 243 units, 3,015.26 BRL of installment interest |

| Reason code | Predicted | Precision | Recall |
|---|---|---|---|
| `MISSING_SOURCE_EVIDENCE` | 775 | 1.00 | 1.00 |
| `ORDER_STATUS_REQUIRES_POLICY` | 1,082 | 1.00 | 1.00 |
| `MISSING_PAYMENT` | 477 | 1.00 | 1.00 |
| `AMOUNT_MISMATCH` | 1,031 | 1.00 | 1.00 |
| `INSTALLMENT_INTEREST` (auto-accepted with a note) | 243 | 1.00 | 1.00 |
| `MISSING_SHIPMENT` | 471 | 1.00 | 1.00 |
| `TIMING_MISMATCH` | 30 | — | — |

Recall rounds to 1.00 for `MISSING_SOURCE_EVIDENCE` (775 of 776). Quality is judged by precision and recall for each reason code, not by overall
accuracy: 96% of orders match, so labelling every order as matched would
already score about 96%.

**Link metrics.** A config can name a link-truth file (`link_truth`: feed, record key, order, link kind). The manifest
then reports:

- **Links:** precision per linking pass and recall per feed and per true link kind.
- **Pass-3 candidates:** how often the true bank line is the best one proposed, or among those proposed.
- **Money:** for each reason code, the amount caught and the amount missed.

On the Olist benchmark, all 196,078 links are made in pass 1 and are correct.

The Vietnamese multi-channel sets (`test/vn`) record a baseline in their manifests. Marketplace orders are settled
through their Shopee and TikTok Shop reports and COD orders through the GHN, GHTK and J&T remittances, so every
settlement line, payout credit and remittance credit is linked, and split transfers link as a group. Accuracy is
96.7–100% with an auto-accept precision of 1.0; the orders still wrong are payer-name cases whose truth is REVIEW,
which the scoring counts as wrong (#43).

**Known gaps in the benchmark:**

- **Simulated interest column:** the real Olist payment total exceeds items + freight on some installment orders: interest on installments. Olist has no interest column, so the generator adds `interest_amount` to the bank feed, as PSP statements report it (real payment total minus the order amount on those orders). Without that column the engine cannot tell interest from an overpayment: a band-only rule would also have auto-accepted 521 of the 977 injected `AMOUNT_ERROR`s. Feeds that report installments but no interest get `POSSIBLE_INSTALLMENT_INTEREST` (REVIEW) instead.
- **Real unexplained deltas:** 54 orders whose real payment differs from items + freight for no known reason are labelled `SOURCE_AMOUNT_DELTA` and expected as `AMOUNT_MISMATCH`.
- **Timing mismatches have no labels:** the 30 picked-after-delivered shipments are real anomalies in the data, but the ground truth has no label for them.
- **Rules the benchmark can't exercise:** the benchmark is 100% prepaid, with one transaction and one shipment per order. The COD, fee, constrained-link and duplicate rules are therefore covered only by the hand-built test fixtures.

## Performance and cost

```powershell
python -m recon.perf report --sizes 100000 1000000 --repeats 10 3
```

The harness copies the Olist benchmark up to each size. Each sample is a fresh process
that runs inspect, run and export, as the app does. Every run also records its stage
timings, CPU time, peak memory and input/output size in `manifest["performance"]`.
Measured on Windows 11, an Intel Ice Lake CPU (8 logical CPUs), Python 3.12, pandas 3.0.
Full results: [reports/performance.md](reports/performance.md).

| Units | Samples | Overall P50 / P95 / P99 (s) | `run` P50 / P95 / P99 (s) | Peak memory | Matching share |
|---|---|---|---|---|---|
| 100,000 | 10 | 40 / 51 / 51 | 22 / 28 / 29 | 0.85 GB | 21% |
| 1,000,000 | 3 | 535 / 554 / 556 | 295 / 330 / 333 | 7.1 GB | 21% |

- **Overall** is what the user waits for: inspect + run + export. At 1M units: inspect 151 s, export 69 s.
- **Matching share** is linking + rule-check time over `run`.
- **Dominant stages:** at 1M units, the checks (29%) and loading (28%) take most of `run`. Linking takes 18%, the decision step 8%, and rule checks 3%.
- **Cost per 1,000 units:** compute + memory is $0.00001 and storage is $0.0005 (98% of the total). The prices are assumptions (AWS Fargate and S3 Standard, us-east-1), and so is the 12-month retention, which drives the storage cost. Replace them with your own in `recon.perf.CostModel`.

## Project layout

```
src/recon/
├── pipeline.py       # public entry point: inspect, run, draft_config, public types
├── config.py         # RunConfig, FeedSpec, Policy, Resolution
├── schema.py         # canonical columns, keys, lineage and decision constants
├── ingest.py         # format readers, encoding/delimiter detection, row hashing, parsing
├── mapping.py        # mapping suggestion, draft config, mapping profiles
├── checks/           # blocking and warning checks (registry)
├── profiles.py       # info-level profiles
├── health.py         # Issue, FixOption, HealthReport
├── resolutions.py    # applies resolutions: remap, exclusions, duplicates, dates, status policy
├── funnel/           # ordered steps: eligibility, linking, routing, rules, decision, orphans
├── results.py        # ReconRun, exception queue, export bundle
├── evaluation.py     # ground truth, per-reason-code metrics
├── olistdata.py      # builds the Olist benchmark feeds
├── perf.py           # runtime and cost harness, writes reports/performance.md
├── timing.py         # stage timer and per-run peak memory
└── mockdata.py       # synthetic feed generator
app.py, app_pages/, recon_app/   # Streamlit wizard over recon.pipeline
configs/olist_benchmark.json     # benchmark run config
notebooks/                       # EDA of the four feeds (Vietnamese)
```

## Tests

```powershell
pytest
```

The tests use the public `inspect` / `run` seam with small hand-built CSV
fixtures, so internal modules can change freely. The suite also includes an
end-to-end regression on the Olist benchmark (deterministic, auto-accept
precision ≥ 0.995, review rate ≤ 1%, no orphans) and Streamlit AppTest smoke
tests of the wizard. The benchmark tests need `data/generated/`; see Quick start.

## Not yet in scope

- **Exception resolution actions:** force match, write-off and refund, with an audit log.
- **Infrastructure:** PostgreSQL sources, and dedup across repeated ingests of the same file.
- **Matching beyond one-to-one:** batches without a settlement report.
- **Packaging:** Docker.
