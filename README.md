# SmartRecon

SmartRecon is a portfolio showcase for precision-first three-way reconciliation:
OMS orders, payment-bank credits, and 3PL logistics events. It combines
deterministic candidate generation with an interpretable discrepancy classifier,
confidence-aware abstention, and an auditable triage queue.

The primary demo uses a seeded BRL reconciliation benchmark derived from the
real Olist records in `brazil ecom/`. Olist supplies the order, item, payment,
and delivery evidence; `recon.olistdata` derives bank and logistics feed
exports from those records and records provenance in `data/generated/manifest.json`.
The generated scenarios are controlled mutations of real rows, not an
independent fictional business domain.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
streamlit run app.py
```

Regenerate the derived benchmark after changing the source data or generator:

```powershell
python -m recon.olistdata --source "brazil ecom" --out data/generated --rows 25000 --seed 42
```

The app has pages for the executive story, data contract, match explorer,
exceptions/triage, model diagnostics, and the Olist source explorer. Every
decision can be downloaded as CSV/JSON, including reason codes and feature
contributions.

## Quality bar

The engine uses grouped, scenario-aware evaluation and exposes the operating
point used by the app: auto-accept only high-confidence matches, abstain on
ambiguous cases, and report precision and review rate together. Run the
automated suite with:

```powershell
pytest
```