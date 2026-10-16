# SmartRecon

Reconciliation of a seller's orders against the money and shipments that should follow them, so a month can be closed with every difference explained.

## Inputs

**Feed**:
One input file with a role: orders, bank statement, settlement report or logistics report.
_Avoid_: source, file (when the role matters)

**Settlement report**:
A platform's or carrier's statement listing, per payout, the orders it covers with their gross, fees and net.
_Avoid_: payout report, escrow report

**Channel**:
Where an order was sold: the seller's website, a marketplace (Shopee, TikTok Shop) or social selling (Facebook).
_Avoid_: platform (when meaning the sales channel), source

## Linking

**Money chain**:
The hops a channel's money takes from an order to the bank credit, with the key that links each hop.
_Avoid_: flow, path, pipeline

**Settlement batch**:
One bank credit that pays many orders at once: a marketplace payout or a carrier's COD remittance.
_Avoid_: payout (for both kinds), batch payment

**Settlement line**:
One order's row in a settlement report: its gross, fees, refund and net, and the payout it is paid in.
_Avoid_: settlement row, escrow line

**Remittance**:
A settlement batch from a carrier: the COD it collected, less shipping and COD fees.
_Avoid_: COD payout

**Split payment**:
One order paid in several bank credits, such as a deposit and the rest; the credits are compared as a total.
_Avoid_: partial payment (for a credit that is simply short)

**Duplicate payment**:
Several bank credits for one order, one of which alone pays it.
_Avoid_: double charge

**Orphan**:
A bank credit, settlement line or shipment that no order claims.
_Avoid_: unmatched record

## Decisions

**Unit**:
One order being reconciled; every unit gets exactly one decision and a reason code.
_Avoid_: case, row, item

**Decision**:
What the engine concludes about a unit: auto-accept, review, exception or open.
_Avoid_: outcome, status, result

**Open item**:
A unit whose money is not yet due at the cut-off; it is carried into the next period.
_Avoid_: pending, in transit (as decision names)

## Periods

**Period**:
The span of orders a run reconciles, ending at a cut-off.
_Avoid_: month (periods need not be months), window

**Cut-off**:
The moment after which money and shipments no longer count for the period.
_Avoid_: statement date, close date
