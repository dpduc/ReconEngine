## Checks

`ruff check .` runs as a pre-commit hook; CI runs it plus `pytest`. The full suite takes ~9 minutes, so run the test files you touched while iterating.

## Agent skills

### Issue tracker

Issues and specs are tracked as GitHub Issues on dpduc/ReconEngine (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
