# Coding standards

Judgement calls for reviewing a diff. Mechanical rules live in tooling instead: `ruff check .` (pyproject.toml) and `tests/test_standards.py`. Terms are defined in `GLOSSARY.md`; design decisions in `docs/adr/`.

## Money

**Amounts and fees are computed in the currency's minor unit.** Expected fees go through `contracts.currency_fee` (0 digits for VND, 2 for BRL/USD, half-up). Truncating an amount with `int()` or rounding a fee to whole units is a violation.
_Why_: #37. R$ 99.90 at 399 bps expected a fee of 4 instead of 3.99, so a correct fee became `FEE_MISMATCH`.

**Tolerances and rates come from `contracts` or the run config.** A literal tolerance, rate or window in engine code is a violation, even when it equals today's value.

## Decisions

**AUTO_ACCEPT is earned on every key both sides carry.** A link path that can auto-accept a unit compares every identifying field present on both records (order ref, amount, date, payer name). A pair that is unique on some keys but untested on an available one goes to REVIEW.
_Why_: #36. Pass 2 linked transfers from a different payer by amount and date alone; set C auto-accept precision fell to 0.84.

**A fallback is never silent.** When a setting is looked up per key (fee rate per payment type, terms per channel) and a key is missing, the run uses the fallback *and* raises a Health warning naming the key.
_Why_: #35. One MDR applied to every prepaid credit flagged each fee-free bank transfer as `FEE_MISMATCH`.

**Every unit gets exactly one decision and a reason code.** A new funnel step names the reason codes it emits, and each code has a plain-language explanation in `funnel/explain.py`.

**A change to decision logic re-records the VN baselines in the same PR**, and the PR states how auto-accept precision and review rate moved per set.

## One source of truth

**Each fact has one home.** Per-feed column lists, decision names, reason codes and fix-option ids are defined once and imported. A second copy, even an identical one, is a violation: copies drift.
_Why_: the review of #2–#10 found the per-feed column maps defined three times, already disagreeing on date-column order.

## Names

**Use the glossary's terms, not its _Avoid_ words**, in identifiers, UI text and docs: *unit* (not row or case), *decision* (not status or outcome), *settlement batch* (not payout, for both kinds), *orphan* (not unmatched record). A new domain concept gets a GLOSSARY.md entry in the same PR.
