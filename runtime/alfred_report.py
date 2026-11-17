"""Complete saved kanban, paginated for Telegram without model or live-agent calls."""
from datetime import datetime
from pathlib import Path
import re

from operator_policy import WORKSPACE as ROOT

from alfred_board import STATES


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


DONE_PAGE = 20
IDENTITY = re.compile(r"\b(?:codex|claude):\S+|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
GROUPED = ("todo", "doing", "blocked", "review")


def lead(row):
    """Display label only: `Report lead`, else a short project-derived name. Never an identity."""
    label = compact(IDENTITY.sub("", row.get("Report lead", ""))).strip(" ;,.-")
    if label:
        return label
    project = compact(IDENTITY.sub("", row.get("Project") or row["name"]))
    project = re.split(r"\s*[;(]|\s+/\s+|\s+—\s+", project)[0].strip(" ,.-") or "Unassigned"
    return (project[:40].rstrip() + "…" if len(project) > 40 else project) + " lead"


def line(row, number):
    state = row["state"]
    next_step = compact(row.get("Next") or row.get("Snapshot") or "Next step unavailable")
    review = compact(row.get("Reviewer") or "Reviewer not recorded") + ": " if state == "review" else ""
    if state == "blocked":
        review = ("Blocked by " + compact(row.get("Blocked by") or "missing input not recorded") + "; resolver: "
                  + compact(row.get("Resolver") or "not recorded") + ". ")
    parked = " [parked]" if row.get("Project mode") == "parked" else ""
    recurring = " [recurring; next " + compact(row.get("Next run", "not set")) + "]" if row.get("Recurrence") else ""
    return f"{number}. {compact(row['name'])}{parked}{recurring} — {review}{next_step}"


def report(root=ROOT, db=None, live_claude=None):
    """Every open card, starting at BACKLOG. Completions belong to /done."""
    rows = cards(root)
    lines = []
    task_number = 0
    for state in STATES[:-1]:
        current = sorted((row for row in rows if row["state"] == state),
                         key=lambda row: (priority(row), row["name"].casefold(), row["path"]))
        lines.append(("\n" if lines else "") + f"{state.upper()} ({len(current)})")
        if state in GROUPED:
            groups = {}
            for row in current:
                groups.setdefault(lead(row), []).append(row)
            ordered = sorted(groups.items(), key=lambda item: (priority(item[1][0]), item[0].casefold()))
        else:
            ordered = [(None, current)]
        for label, members in ordered:
            if label and members:
                lines.append(label)
            for row in members:
                task_number += 1
                lines.append(line(row, task_number))
        if not current:
            lines.append("• None")
    if db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_dispatch'").fetchone():
        count = db.execute("SELECT COUNT(*) FROM appserver_dispatch d JOIN requests r ON r.id=d.request_id "
                           "WHERE (d.error LIKE 'recovery_required:%' OR d.state IN ('rejected','uncertain')) "
                           "AND r.status IN ('pending','processing')").fetchone()[0]
        issues = db.execute("SELECT COUNT(*) FROM outbox WHERE status IN ('failed','uncertain')").fetchone()[0]
        notes = ([f"{issues} repl{'y' if issues == 1 else 'ies'} not confirmed as delivered" ] if issues else []) + (
            [f"{count} message{'' if count == 1 else 's'} whose processing ended without a recorded reply"] if count else [])
        if notes:
            lines.append("\nNeeds Chief's review: " + "; ".join(notes) + ". Nothing is resent or repeated automatically; "
                         "Chief closes each one only with evidence.")
    lines.append("\nAll open cards are included. Completions: /done. No live checks or model calls.")
    return "\n".join(lines)


def completed(rows):
    """Saved completions, newest first, including completed recurring runs."""
    def stamp(row):
        return saved_time(row, "Last completed") if row.get("Last completed") else saved_time(row)
    return sorted((row for row in rows if row["state"] == "done" or row.get("Last completed")),
                  key=lambda row: (stamp(row), row["path"]), reverse=True), stamp


def done(root=ROOT, page=1):
    """One saved page of /done. Pages are 1-based; an unavailable page is answered locally."""
    rows, stamp = completed(cards(root))
    pages = max(1, -(-len(rows) // DONE_PAGE))
    if not isinstance(page, int) or not 1 <= page <= pages:
        return f"Done page unavailable. Done has {pages} page{'s' if pages != 1 else ''}. Use /done or /done 2."
    first = (page - 1) * DONE_PAGE
    shown = rows[first:first + DONE_PAGE]
    lines = [f"DONE — page {page} of {pages} ({first + 1 if shown else 0}–{first + len(shown)} of {len(rows)}, newest first)"]
    for number, row in enumerate(shown, first + 1):
        when = stamp(row)
        date = when.strftime("%Y-%m-%d") if when != datetime.min else "date unknown"
        summary = compact(row.get("Last done") or row.get("Snapshot") or "Outcome not recorded")
        lines.append(f"{number}. {compact(row['name'])} ({date}) — {summary}")
    if not rows:
        lines.append("• None")
    navigation = ([f"Previous: /done {page - 1}" if page > 2 else "Previous: /done"] if page > 1 else []) + \
                 ([f"Next: /done {page + 1}"] if page < pages else [])
    lines.append("\n" + (" · ".join(navigation) if navigation else "No more pages.") + " Saved records only; no model calls.")
    return "\n".join(lines)


def report_pages(root=ROOT, db=None):
    """Never drop cards to fit one message; each page stays within Telegram's UTF-16 limit."""
    from telegram_bridge import reply_parts
    return reply_parts("result:report:preview", report(root, db))
