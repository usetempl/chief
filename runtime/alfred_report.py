"""Complete saved kanban, paginated for Telegram without model or live-agent calls."""
from datetime import datetime
from pathlib import Path
import re

from operator_policy import WORKSPACE as ROOT

STATES = ("backlog", "todo", "doing", "review", "done")


def active_claude():
    """Compatibility for collectors started before the saved-only report update."""
    return None


def compact(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text


def cards(root):
    rows = []
    for state in STATES:
        for path in sorted((Path(root) / "tasks" / state).rglob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            fields = {}
            try:
                content = path.read_text(encoding="utf-8")
                title = next((line[2:] for line in content.splitlines() if line.startswith("# ")), path.stem)
                for line in content.splitlines():
                    match = re.match(r"^([^:]+):\s*(.*)$", line)
                    if match:
                        fields.setdefault(match[1], match[2])
            except (OSError, UnicodeError):
                title = path.stem
                fields["Next"] = "Unreadable card; repair the saved record."
            fields.update(state=state, name=fields.get("Report name") or title, path=str(path))
            rows.append(fields)
    return rows


def priority(row):
    try:
        return int(row.get("Priority", 999))
    except ValueError:
        return 999


def saved_time(row, field="Updated"):
    match = re.match(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", row.get(field, ""))
    try:
        return datetime.fromisoformat(match[0]) if match else datetime.min
    except ValueError:
        return datetime.min


def report(root=ROOT, db=None, live_claude=None):
    rows = cards(root)
    lines = ["Chief kanban — saved task board", "Use the task numbers from this report; the next report can renumber them.",
             "Backlog: uncommitted ideas/research. Todo: committed next work or scheduled recurrence. Review: a named human's decision."]
    task_number = 0
    for state in STATES[:-1]:
        current = sorted((row for row in rows if row["state"] == state),
                         key=lambda row: (priority(row), row["name"].casefold(), row["path"]))
        lines.append(f"\n{state.upper()} ({len(current)})")
        for row in current:
            next_step = compact(row.get("Next") or row.get("Snapshot") or "Next step unavailable")
            review = compact(row.get("Reviewer") or "Reviewer not recorded") + ": " if state == "review" else ""
            parked = " [parked]" if row.get("Project mode") == "parked" else ""
            recurring = " [recurring; next " + compact(row.get("Next run", "not set")) + "]" if row.get("Recurrence") else ""
            task_number += 1
            lines.append(f"{task_number}. {compact(row['name'])}{parked}{recurring} — {review}{next_step}")
        if not current:
            lines.append("• None")
    done = sorted((row for row in rows if row["state"] == "done" or row.get("Last completed")),
                  key=lambda row: (saved_time(row, "Last completed") if row.get("Last completed") else saved_time(row), row["path"]), reverse=True)
    lines.append(f"\nDONE — latest {min(5, len(done))} of {len(done)}")
    for row in done[:5]:
        stamp = saved_time(row, "Last completed") if row.get("Last completed") else saved_time(row)
        date = stamp.strftime("%Y-%m-%d") if stamp != datetime.min else "date unknown"
        summary = compact(row.get("Last done") or row.get("Snapshot") or "Outcome not recorded")
        task_number += 1
        lines.append(f"{task_number}. {compact(row['name'])} ({date}) — {summary}")
    if not done:
        lines.append("• None")
    if db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_dispatch'").fetchone():
        count = db.execute("SELECT COUNT(*) FROM appserver_dispatch d JOIN requests r ON r.id=d.request_id "
                           "WHERE (d.error LIKE 'recovery_required:%' OR d.state IN ('rejected','uncertain')) "
                           "AND r.status IN ('pending','processing')").fetchone()[0]
        issues = db.execute("SELECT COUNT(*) FROM outbox WHERE status IN ('failed','uncertain')").fetchone()[0]
        lines.append(f"\nDelivery: {issues} flagged replies; {count} requests need reconciliation. Nothing is replayed automatically.")
    lines.append("\nAll open cards are included. No live checks or model calls.")
    return "\n".join(lines)


def report_pages(root=ROOT, db=None):
    """Never drop cards to fit one message; each page stays within Telegram's UTF-16 limit."""
    from telegram_bridge import text_parts
    return text_parts(report(root, db))
