# SmartRecon

SmartRecon is a portfolio showcase for precision-first three-way reconciliation:
OMS orders, payment-bank credits, and 3PL logistics events. It combines
deterministic candidate generation with an interpretable discrepancy classifier,
confidence-aware abstention, and an auditable triage queue.

The primary demo uses the deterministic VND fixtures in `data/generated/`.
The `brazil ecom/` Olist files are intentionally presented as a separate
data-quality explorer because they describe a different business domain.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
streamlit run app.py
```

The app has pages for the executive story, data contract, match explorer,
exceptions/triage, model diagnostics, and the Brazil Olist explorer. Every
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