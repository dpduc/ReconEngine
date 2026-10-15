---
status: accepted
---

# Reconcile a period with a cut-off, and carry open items forward

Each run currently reconciles its files in isolation. An order whose money is due after the bank statement ends counts as an exception. A credit in the statement for last month's orders becomes an orphan (130 in Vietnamese set C). A real month-end close treats "not paid yet, but not due yet" as normal and finishes last month's open items first.

We decided that a run reconciles a **period** that ends at a **cut-off**. An order whose money is not yet due at the cut-off is an **open item**, a fourth decision next to AUTO_ACCEPT, REVIEW and EXCEPTION. "Due" is the order's expected settlement date from its money chain's terms (e.g. 3 days after delivery, then the next payout day) plus a grace period. An open item past that date at the cut-off is an EXCEPTION.

The export of each run includes its open items. The next run takes them as an input, so credits for last month's orders match them instead of becoming orphans. Open items cleared this way are reported as such.

## Considered options

- **Load a wider OMS window** (last month's orders too). Rejected: orders get decided twice, and two runs can disagree about the same order.
- **Keep them as exceptions** (today). Rejected: every month-end shows a predictable pile of false exceptions that a person must clear by hand.

## Consequences

- **Four decisions:** the decision set becomes AUTO_ACCEPT, REVIEW, EXCEPTION and OPEN.
- **Chained runs:** a run can take the previous run's open items as an input, so runs form a chain.
- **Ground truth:** it needs an expected OPEN decision. Today's `PENDING_SETTLEMENT` (expected EXCEPTION) becomes OPEN.
- **Expected REVIEW:** ground truth also needs an expected REVIEW, for cases where a person must choose. Otherwise accuracy counts every REVIEW as wrong.
