# Performance and cost

Measured on Windows 11, Intel64 Family 6 Model 126 Stepping 5, GenuineIntel, 8 logical CPUs, Python 3.12.7, pandas 3.0.6. Each sample is a fresh process running inspect, run and export, as the app does. Percentiles are over the samples listed; with few samples P95/P99 are close to the maximum.

## Time per reconciliation (seconds)

Overall is what the user waits for: inspect + run + export. `run` is load, checks, funnel and reverse pass.

| Units | Samples | Measure | P50 | P95 | P99 |
|---|---|---|---|---|---|
| 100,000 | 10 | Overall | 40.486 | 50.545 | 51.294 |
| 100,000 | 10 | Inspect | 11.670 | 17.003 | 17.332 |
| 100,000 | 10 | Run | 22.123 | 28.126 | 28.592 |
| 100,000 | 10 | Export | 5.270 | 8.197 | 8.609 |
| 1,000,000 | 3 | Overall | 534.830 | 553.962 | 555.662 |
| 1,000,000 | 3 | Inspect | 151.252 | 180.715 | 183.334 |
| 1,000,000 | 3 | Run | 294.802 | 329.864 | 332.981 |
| 1,000,000 | 3 | Export | 68.553 | 87.642 | 89.339 |

## Throughput and memory

Matching share is the time of the funnel's linking and rule-check steps over the wall time of `run` (median over samples); the decision and policy/routing steps are not matching.

| Units | Matching share | Peak memory (GB) | Units / s (run P50) | Units / s (overall P50) |
|---|---|---|---|---|
| 100,000 | 21% | 0.85 | 4,520 | 2,470 |
| 1,000,000 | 21% | 7.13 | 3,392 | 1,870 |

## Where `run` time goes (median share of `run`)

Funnel steps are grouped as in `recon.funnel.STEP_GROUPS`; other stages are listed by name, and `untimed` is `run` time outside every recorded stage.

| Units | Group | Share |
|---|---|---|
| 100,000 | `load` | 27% |
| 100,000 | `resolutions` | 0% |
| 100,000 | `raw_checks` | 1% |
| 100,000 | `parse_numeric` | 1% |
| 100,000 | `checks` | 28% |
| 100,000 | `profiles` | 1% |
| 100,000 | `build_units` | 0% |
| 100,000 | `policy/routing` | 4% |
| 100,000 | `linking` | 17% |
| 100,000 | `rule checks` | 3% |
| 100,000 | `decision` | 8% |
| 100,000 | `reverse_pass` | 0% |
| 100,000 | `ground_truth` | 2% |
| 100,000 | `untimed` | 7% |
| 1,000,000 | `load` | 28% |
| 1,000,000 | `resolutions` | 0% |
| 1,000,000 | `raw_checks` | 1% |
| 1,000,000 | `parse_numeric` | 1% |
| 1,000,000 | `checks` | 28% |
| 1,000,000 | `profiles` | 0% |
| 1,000,000 | `build_units` | 0% |
| 1,000,000 | `policy/routing` | 4% |
| 1,000,000 | `linking` | 18% |
| 1,000,000 | `rule checks` | 3% |
| 1,000,000 | `decision` | 8% |
| 1,000,000 | `reverse_pass` | 1% |
| 1,000,000 | `ground_truth` | 2% |
| 1,000,000 | `untimed` | 7% |

## Slowest stages at 1,000,000 units (median seconds, share of `run`)

| Stage | Seconds | Share |
|---|---|---|
| `checks` | 85.65 | 29% |
| `load` | 82.18 | 28% |
| `funnel:link_constrained` | 27.19 | 9% |
| `funnel:link_candidates` | 19.68 | 7% |
| `funnel:decision` | 19.16 | 6% |
| `funnel:status_policy` | 6.70 | 2% |
| `funnel:link_exact` | 6.23 | 2% |
| `funnel:missing_source_evidence` | 5.54 | 2% |

## Cost per reconciliation

Assumed prices (replace with your own): `vcpu_hour` = 0.04048, `gb_hour` = 0.004445, `storage_gb_month` = 0.023, `retention_months` = 12, `api_per_run` = 0.0. Compute is the CPU-seconds of inspect, run and export (not process startup or imports); memory is the peak memory held for the wall time; storage keeps the inputs and the export bundle for the retention period, which is an assumption. No external API is called. Costs are medians over samples; per 1,000 units divides by the units each sample measured.

| Units | Compute ($) | Memory ($) | Storage ($) | API ($) | Total per run ($) | Compute + memory per 1,000 units ($) | Storage per 1,000 units ($) | Total per 1,000 units ($) | Compute / memory / storage share |
|---|---|---|---|---|---|---|---|---|---|
| 100,000 | 0.00044 | 0.00004 | 0.05054 | 0.00000 | 0.05101 | 0.000005 | 0.000505 | 0.000510 | 1% / 0% / 99% |
| 1,000,000 | 0.00539 | 0.00462 | 0.50640 | 0.00000 | 0.51640 | 0.000010 | 0.000506 | 0.000516 | 1% / 1% / 98% |

At 1,000,000 units, storage is 98% of the cost per run; it follows the assumed retention of 12 months, so compare compute + memory per 1,000 units for the cost of reconciling itself.

## Appendix: time per stage (seconds)

Stages in pipeline order; percentiles are over the samples that ran the stage.

| Units | Stage | P50 | P95 | P99 |
|---|---|---|---|---|
| 100,000 | `load` | 5.584 | 7.437 | 7.791 |
| 100,000 | `resolutions` | 0.001 | 0.004 | 0.004 |
| 100,000 | `raw_checks` | 0.292 | 0.392 | 0.403 |
| 100,000 | `parse_numeric` | 0.249 | 0.391 | 0.421 |
| 100,000 | `checks` | 5.684 | 7.771 | 7.944 |
| 100,000 | `profiles` | 0.121 | 0.168 | 0.175 |
| 100,000 | `build_units` | 0.002 | 0.003 | 0.003 |
| 100,000 | `funnel:missing_source_evidence` | 0.412 | 0.502 | 0.513 |
| 100,000 | `funnel:status_policy` | 0.478 | 0.620 | 0.623 |
| 100,000 | `funnel:link_exact` | 0.474 | 0.589 | 0.593 |
| 100,000 | `funnel:link_constrained` | 1.777 | 2.997 | 3.410 |
| 100,000 | `funnel:link_candidates` | 1.404 | 3.132 | 3.894 |
| 100,000 | `funnel:route_review` | 0.000 | 0.001 | 0.001 |
| 100,000 | `funnel:missing_payment` | 0.402 | 0.788 | 0.942 |
| 100,000 | `funnel:currency_mismatch` | 0.008 | 0.022 | 0.025 |
| 100,000 | `funnel:fx_difference` | 0.010 | 0.020 | 0.022 |
| 100,000 | `funnel:amount_mismatch` | 0.107 | 0.168 | 0.180 |
| 100,000 | `funnel:missing_shipment` | 0.084 | 0.125 | 0.132 |
| 100,000 | `funnel:fee_mismatch` | 0.009 | 0.017 | 0.021 |
| 100,000 | `funnel:cod_fee_mismatch` | 0.009 | 0.014 | 0.014 |
| 100,000 | `funnel:timing_mismatch` | 0.067 | 0.095 | 0.097 |
| 100,000 | `funnel:possible_installment_interest` | 0.009 | 0.016 | 0.018 |
| 100,000 | `funnel:decision` | 1.584 | 2.728 | 3.014 |
| 100,000 | `reverse_pass` | 0.073 | 0.111 | 0.119 |
| 100,000 | `ground_truth` | 0.497 | 0.695 | 0.750 |
| 100,000 | `export` | 5.229 | 8.142 | 8.550 |
| 1,000,000 | `load` | 82.179 | 84.703 | 84.927 |
| 1,000,000 | `resolutions` | 0.002 | 0.002 | 0.003 |
| 1,000,000 | `raw_checks` | 4.033 | 4.425 | 4.460 |
| 1,000,000 | `parse_numeric` | 3.493 | 7.170 | 7.497 |
| 1,000,000 | `checks` | 85.646 | 91.907 | 92.463 |
| 1,000,000 | `profiles` | 1.492 | 1.626 | 1.638 |
| 1,000,000 | `build_units` | 0.007 | 0.008 | 0.008 |
| 1,000,000 | `funnel:missing_source_evidence` | 5.535 | 6.282 | 6.348 |
| 1,000,000 | `funnel:status_policy` | 6.703 | 7.886 | 7.991 |
| 1,000,000 | `funnel:link_exact` | 6.234 | 7.331 | 7.428 |
| 1,000,000 | `funnel:link_constrained` | 27.186 | 29.258 | 29.442 |
| 1,000,000 | `funnel:link_candidates` | 19.679 | 22.007 | 22.214 |
| 1,000,000 | `funnel:route_review` | 0.001 | 0.001 | 0.001 |
| 1,000,000 | `funnel:missing_payment` | 4.976 | 5.763 | 5.834 |
| 1,000,000 | `funnel:currency_mismatch` | 0.017 | 0.022 | 0.023 |
| 1,000,000 | `funnel:fx_difference` | 0.014 | 0.028 | 0.029 |
| 1,000,000 | `funnel:amount_mismatch` | 1.569 | 1.717 | 1.730 |
| 1,000,000 | `funnel:missing_shipment` | 1.257 | 1.427 | 1.442 |
| 1,000,000 | `funnel:fee_mismatch` | 0.017 | 0.018 | 0.018 |
| 1,000,000 | `funnel:cod_fee_mismatch` | 0.014 | 0.020 | 0.021 |
| 1,000,000 | `funnel:timing_mismatch` | 1.223 | 1.529 | 1.556 |
| 1,000,000 | `funnel:possible_installment_interest` | 0.015 | 0.020 | 0.020 |
| 1,000,000 | `funnel:decision` | 19.161 | 25.252 | 25.794 |
| 1,000,000 | `reverse_pass` | 1.597 | 2.571 | 2.658 |
| 1,000,000 | `ground_truth` | 4.628 | 9.436 | 9.864 |
| 1,000,000 | `export` | 68.052 | 87.018 | 88.703 |
