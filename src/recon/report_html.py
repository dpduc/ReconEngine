"""Standalone HTML rendering of a Health Report: inline CSS, no scripts, nothing fetched."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from recon.health import HealthReport

STYLE = """
:root { color-scheme: light dark; --fg: #1d2025; --muted: #5f6672; --bg: #ffffff; --line: #d9dce1;
        --blocking: #b3261e; --warning: #8a5a00; --info: #2f5f98; --ok: #24723a; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e7e9ec; --muted: #a2a8b3; --bg: #16181c; --line: #343840;
          --blocking: #f2867f; --warning: #e7b85c; --info: #8db7ea; --ok: #7fd093; } }
body { font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
h1 { margin: 0 0 4px; } h2 { margin-top: 32px; } .muted { color: var(--muted); }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 14px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.scroll { overflow-x: auto; } code { font-size: 13px; word-break: break-all; }
.sev-blocking { color: var(--blocking); font-weight: 600; } .sev-warning { color: var(--warning); font-weight: 600; }
.sev-info { color: var(--info); } .open { color: var(--blocking); } .closed { color: var(--ok); }
"""


def _cell(value: Any) -> str:
    return "" if value is None else escape(f"{value:,.2f}" if isinstance(value, float) else str(value))


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="muted">None.</p>'
    columns = list(dict.fromkeys(key for row in rows for key in row))
    head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_cell(row.get(c))}</td>" for c in columns) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _profile(value: Any) -> str:
    if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
        return _table([{"series": key, **stats} for key, stats in value.items()])
    if isinstance(value, dict):
        return _table([{"key": key, "value": v} for key, v in value.items()])
    return f"<p>{_cell(value)}</p>"


def _guidance(issue) -> str:
    from recon.guidance import guide

    advice = guide(issue).to_dict(issue)
    kind = "Needs a decision" if advice["needs_decision"] else "Sign-off only"
    recommended = (f"<p><strong>Recommended:</strong> {escape(advice['recommended_option'])}. "
                   f"{escape(advice['why'])}</p>" if advice["recommended_option"] else "")
    caution = f'<p class="open">{escape(advice["caution"])}</p>' if advice["caution"] else ""
    return (f"<h3><code>{escape(issue.id)}</code> · {kind}</h3><p>{escape(advice['meaning'])}</p>"
            f"<p>{escape(advice['next_step'])}</p>{recommended}{caution}")


def render_health_report(report: HealthReport, title: str = "Health Report") -> str:
    open_ids = {issue.id for issue in report.open_issues}
    gate = ('<p class="closed">Gate open: the run can proceed.</p>' if report.gate_open else
            f'<p class="open">Gate closed: {len(open_ids)} issue(s) need a resolution or acknowledgement.</p>')
    issue_rows = []
    for issue in report.issues:
        status = "open" if issue.id in open_ids else (
            "acknowledged" if report.is_acknowledged(issue) else "informational")
        issue_rows.append(
            f'<tr><td class="sev-{issue.severity.value}">{escape(issue.severity.value)}</td>'
            f"<td><code>{escape(issue.id)}</code></td><td>{escape(issue.feed)}</td>"
            f"<td>{issue.affected_rows:,}</td><td>{escape(issue.message)}</td><td>{status}</td>"
            f"<td>{escape(report.acknowledged.get(issue.id, '')) if status == 'acknowledged' else ''}</td></tr>")
    issues = ('<div class="scroll"><table><thead><tr><th>Severity</th><th>Issue</th><th>Feed</th><th>Rows</th>'
              "<th>Message</th><th>Status</th><th>Note</th></tr></thead><tbody>"
              + "".join(issue_rows) + "</tbody></table></div>") if issue_rows else '<p class="muted">No issues.</p>'
    advice = "".join(_guidance(issue) for issue in report.issues if issue.id in open_ids)
    samples = "".join(
        f"<h3><code>{escape(issue.id)}</code></h3>{_table([dict(row) for row in issue.sample_rows])}"
        for issue in report.issues if issue.sample_rows)
    profiles = "".join(f"<h3>{escape(name)}</h3>{_profile(value)}" for name, value in report.profiles.items())
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>"
        f"<h1>{escape(title)}</h1>{gate}"
        f"<h2>Issues</h2>{issues}"
        + (f"<h2>What to do</h2>{advice}" if advice else "") +
        f"<h2>Profiles</h2>{profiles or '<p class=\"muted\">No profiles.</p>'}"
        f"<h2>Sample rows</h2>{samples or '<p class=\"muted\">No samples.</p>'}"
        "</body></html>\n"
    )
