---
status: proposed
---

# Reconcile each channel along its money chain, keeping the order as the unit

Today the engine takes exactly three feeds (OMS, bank, logistics) and links each order one-to-one to a bank credit. That fits a prepaid website, but marketplace and COD money arrives as one bank credit for many orders. The detail of that credit is in a settlement report or a carrier remittance, not in the bank line. On the Vietnamese test sets, 1,594 of 2,104 wrong decisions are orders whose money arrived in such a batch.

We decided that a run config declares any number of named feeds, each with a role (orders, bank, settlement report, logistics). Each sales channel declares its **money chain**: the hops from an order to the bank credit, and the key that links each hop.

```text
website prepaid : order ──order ref──▶ bank credit
marketplace     : order ──channel order no──▶ settlement line ──payout id──▶ bank credit
COD             : order ──order ref──▶ shipment ──remittance id──▶ bank credit
```

The **order stays the unit** that gets a decision and a reason code. A **settlement batch** (payout or remittance) is a second checked object with its own result:

- **Batch tie-out:** the bank credit equals the sum of its lines' net.
- **Line check:** each order is checked against its own line (gross equals order amount, fees as the channel's terms say).

A wrong fee is reported on the line that carries it. A batch that doesn't tie out is reported on the batch, and every order in it goes to REVIEW.

## Considered options

- **Many-to-many links inside today's order-first funnel** (the original #21). Rejected: every channel shape becomes a special case in the linking passes, and the settlement report has no place to load.
- **The bank credit as the unit.** Rejected: the question the seller must answer at month end is "was every order paid?", and exceptions are worked per order. Reporting the batch separately keeps both views.

## Consequences

- **Feeds and keys come from config:**
  - The fixed `FEEDS` tuple goes.
  - Health checks run per feed role.
  - The order reference pattern in the bank description is set per chain, not hard-coded to our generators' formats.
- **Policy per channel:** fee terms, date window and tolerance can be set per channel and payment type, with today's global policy as the default.
- **Today's behaviour is one chain:** a config without channels is a single website-prepaid chain, so existing configs and the Olist benchmark behave as today.
- **Batches without a report:** a batch whose lines can't be found is matched by a bounded subset-sum search, and its candidates always go to REVIEW.
