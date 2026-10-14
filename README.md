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
| **Load** | Upload the OMS, bank and logistics files (CSV, XLSX or Parquet), or point at a **local folder** and pick a file for each feed. Uploads stay in memory and folder files are read in place, so no copies are written to disk. Local-folder mode only works when the app runs on the machine that holds the folder. The engine suggests a column mapping from column names and value patterns, and flags any suggestion it isn't sure of; nothing is mapped silently. A confirmed mapping can be saved as a profile and reused. |
| **Health** | The Health Report groups issues by severity (blocking / warning / info), with affected-row counts, sample rows and profiles. It can be downloaded as JSON or as a standalone HTML page. |
| **Fix** | Pick a fix option for each issue, or acknowledge a warning with a note. Each choice is written into the run config and the checks run again. The run config can be downloaded. |
| **Run** | Available only once the gate is open. Runs the funnel and shows the counts at each step. |
| **Review** | The exception queue (EXCEPTION and REVIEW units plus orphan records) is ranked by amount at risk, then age. For any unit you can see its OMS, bank and 3PL rows side by side, and download the full results bundle. |

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
outcome.bundle()         # decisions, triage queue, exception queue, orphans, funnel, excluded rows, manifest
```

### Run config

A run config is a JSON file (or YAML, if PyYAML is installed) with these parts:

- `feeds`: path, format, encoding, date format and delimiter for each feed.
- `mapping`: canonical column → source column, for each feed.
- `policy`: absolute amount tolerance (default 0.01), date window for linking (default ±4 days), fee rates, and a `status_policy` that says which order statuses need payment and/or shipment evidence.
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
| Warning | missing or zero amounts, coverage gaps, picked up after delivered, placeholder 00:00:00 timestamps, amount outliers, order statuses not covered by the status policy, bank credits in another currency than their order | acknowledge with a note, set the status policy; for currency mismatches: correct the currency code, route to review, or exclude as a last resort |
| Info | amount distributions, order-status mix, currency mix per feed, txn → value-date lag percentiles | none |

Excluded rows are never dropped silently. They are reported in the run as a separate category, with their reason.

A feed may mix currencies. Each linked order and credit are compared in their own currency, and a credit whose currency differs from its order's is never compared as if it were the same currency: it becomes `CURRENCY_MISMATCH`.

## The funnel

Each step only sees the units that earlier steps left undecided. Eligibility
comes before evidence. Otherwise orders with missing amounts would show up as
amount mismatches, and orders still waiting on a status policy would show up as
missing shipments.

| # | Step | Decides |
|---|---|---|
| 1 | `missing_source_evidence` | `MISSING_SOURCE_EVIDENCE`: no order amount |
| 2 | `status_policy` | `ORDER_STATUS_REQUIRES_POLICY`: a status the policy doesn't cover |
| 3 | `link_exact` | links one-to-one on references (structured `order_ref`, or extracted from the bank description) |
| 4 | `link_constrained` | links on same currency, amount within tolerance and date within the window. Never auto-matches when several candidates qualify |
| 5 | `link_candidates` | REVIEW with `LINK_CANDIDATES`: proposes candidates with a confidence score, never auto-accepted |
| 6 | `route_review` | REVIEW: duplicate keys whose strategy is "route to review" |
| 7 | rule checks | `MISSING_PAYMENT` → `CURRENCY_MISMATCH` → `AMOUNT_MISMATCH` → `MISSING_SHIPMENT` → fee / COD fee → `TIMING_MISMATCH` |
| 8 | `decision` | `AUTO_ACCEPT` |
| 9 | `reverse_pass` | `ORPHAN_PAYMENT` / `ORPHAN_SHIPMENT`: bank or 3PL records that link to no order |

Each unit gets one reason code (the first check it fails) and a plain-language
explanation. It also records the step that decided it, the rule version and the
hashes of the input rows behind it.

## Benchmark results

Results on the full Olist benchmark (99,441 orders, every warning acknowledged):

| Metric | Value |
|---|---|
| Auto-accepted | 95,332 (95.9%) |
| Auto-accept precision | 1.000 |
| Review rate | 0.0% |
| Exceptions | 4,109 |
| Orphans | 0 |

| Reason code | Predicted | Precision | Recall |
|---|---|---|---|
| `MISSING_SOURCE_EVIDENCE` | 775 | 1.00 | 1.00 |
| `ORDER_STATUS_REQUIRES_POLICY` | 1,082 | 1.00 | 1.00 |
| `MISSING_PAYMENT` | 477 | 1.00 | 1.00 |
| `AMOUNT_MISMATCH` | 1,274 | 0.77 | 1.00 |
| `MISSING_SHIPMENT` | 471 | 1.00 | 1.00 |
| `TIMING_MISMATCH` | 30 | — | — |

Recall rounds to 1.00 for `MISSING_SOURCE_EVIDENCE` (775 of 776). Quality is judged by precision and recall for each reason code, not by overall
accuracy: 96% of orders match, so labelling every order as matched would
already score about 96%.

**Known gaps in the benchmark:**

- **Amount mismatches the ground truth calls matched:** 297 of the `AMOUNT_MISMATCH` predictions are orders the ground truth labels as matched, yet their amounts really differ. A tolerance can't absorb them without missing real `AMOUNT_ERROR`s, which are always exactly 5% and at least 1.00 BRL. They may be vouchers or instalment interest, which needs confirmation from the business.
- **Timing mismatches have no labels:** the 30 picked-after-delivered shipments are real anomalies in the data, but the ground truth has no label for them.
- **Rules the benchmark can't exercise:** the benchmark is 100% prepaid, with one transaction and one shipment per order. The COD, fee, constrained-link and duplicate rules are therefore covered only by the hand-built test fixtures.

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
- **Matching beyond one-to-one:** currency conversion, and many-to-many matching (split payments, batched remittances).
- **Packaging:** Docker.
