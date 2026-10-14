# Performance and cost

Measured on Windows 11, Intel64 Family 6 Model 126 Stepping 5, GenuineIntel, 8 logical CPUs, Python 3.12.7, pandas 3.0.6. Each sample is a fresh process running inspect, run and export, as the app does. Percentiles are over the samples listed; with few samples P95/P99 are close to the maximum.

## Reconcile time (`run`: load, checks, funnel, reverse pass)

| Units | Samples | P50 (s) | P95 (s) | P99 (s) | Inspect P50 (s) | Export P50 (s) | Matching share | Peak memory (GB) | Units / s |
|---|---|---|---|---|---|---|---|---|---|
| 100,000 | 10 | 31.14 | 39.66 | 42.44 | 19.57 | 7.56 | 34% | 0.82 | 3,212 |
| 1,000,000 | 3 | 249.26 | 262.92 | 264.14 | 147.94 | 57.63 | 29% | 6.63 | 4,012 |

## Slowest stages at 1,000,000 units (median seconds, share of `run`)

| Stage | Seconds | Share |
|---|---|---|
| `load` | 77.90 | 31% |
| `checks` | 68.70 | 28% |
| `funnel:decision` | 18.86 | 8% |
| `funnel:link_constrained` | 16.21 | 7% |
| `funnel:link_candidates` | 15.33 | 6% |
| `funnel:link_exact` | 5.31 | 2% |
| `funnel:status_policy` | 5.20 | 2% |
| `ground_truth` | 4.50 | 2% |

## Cost per reconciliation

Assumed prices (replace with your own): `vcpu_hour` = 0.04048, `gb_hour` = 0.004445, `storage_gb_month` = 0.023, `retention_months` = 12, `api_per_run` = 0.0. Compute is CPU-seconds of the whole process; memory is peak memory held for the wall time; storage keeps inputs and the export bundle for the retention period. No external API is called.

| Units | Compute ($) | Memory ($) | Storage ($) | API ($) | Total per run ($) | Cost per 1,000 units ($) |
|---|---|---|---|---|---|---|
| 100,000 | 0.00062 | 0.00006 | 0.05054 | 0.00000 | 0.05122 | 0.000512 |
| 1,000,000 | 0.00508 | 0.00381 | 0.50640 | 0.00000 | 0.51532 | 0.000515 |
