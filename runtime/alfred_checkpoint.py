#!/usr/bin/env python3
"""One on-demand, reviewed checkpoint for a batch of answered assignments.

`prepare` reads each request's delivery receipts once, runs the installation's
own export and verification commands once, and builds the exact diff of only
the declared paths in a private temporary Git index. The operator reviews that
diff; `confirm --digest` commits exactly those bytes and, when configured,
pushes only that commit to the pinned remote URLs.
Every step is rechecked and resumable: a changed byte, moved HEAD, fresh
redaction finding, non-durable delivery or diverged remote stops with the fact.
The operator's own index, other staged files and the worktree are untouched.
There is no schedule; nothing runs unless asked.

Optional owner-only `.private/checkpoint.json`:
  {"export": [["/abs/private/export-script", "arg"]], "verify": [["/abs/private/check"]],
   "known_gaps": ["text"], "paths": ["tasks", "projects.md"],
   "remote": "origin", "fetch_url": "EXPECTED", "push_url": "EXPECTED", "push": false}
Commands are argument arrays from the installation's existing scripts, run from
the workspace without a shell and without a time limit. They are referenced,
never copied into source. A failed command's complete output stays owner-only
under .private/checkpoints. A remote is used only when its configured fetch and
push URLs are exactly the expected ones, at prepare, confirm and recovery.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import time

from history_safety import redact
from operator_policy import WORKSPACE
from telegram_bridge import BridgeError, Inbox, ROOT

ALLOWED = {"email", "long_token"}  # As for replies: no credential-like content.
EMPTY = "0" * 40


def load_config(root):
    path = Path(root) / "checkpoint.json"
    if not path.exists():
        return {}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("checkpoint.json must be an owner-only regular file")
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            raise ValueError
        for name in ("export", "verify"):
            commands = config.get(name, [])
            if not isinstance(commands, list) or not all(
                    isinstance(argv, list) and argv and all(isinstance(part, str) and part for part in argv) for argv in commands):
                raise ValueError
        if not all(isinstance(item, str) for item in config.get("known_gaps", []) + config.get("paths", [])):
            raise ValueError
        if not isinstance(config.get("remote", ""), str) or not isinstance(config.get("push", False), bool):
            raise ValueError
        if config.get("remote") and not all(isinstance(config.get(key), str) and config[key] for key in ("fetch_url", "push_url")):
            raise ValueError  # Never an implicit target.
        if config.get("push") and not config.get("remote"):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("Invalid checkpoint.json") from None
    return config


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
        digest TEXT PRIMARY KEY, state TEXT NOT NULL, record TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")


def git(workspace, *args, index=None, data=None, check=True):
    env = dict(os.environ, LC_ALL="C", GIT_TERMINAL_PROMPT="0")
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(["git", "-C", str(workspace), *args], input=data, capture_output=True, env=env,
                            stdin=None if data is not None else subprocess.DEVNULL)
    if check and result.returncode:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = redact(lines[-1], conservative=False)[0] if lines else ""
        raise BridgeError("git " + args[0] + " failed" + (": " + detail if detail else ""))
    return result


def out(workspace, *args, **kwargs):
    return git(workspace, *args, **kwargs).stdout.decode().strip()


def declared(paths):
    clean = []
    for raw in paths:
        path = PurePosixPath(raw)
        if (not raw or path.is_absolute() or ".." in path.parts or not path.parts
                or path.parts[0] in {".private", ".venv", ".git"}):
            raise BridgeError("Declare workspace-relative paths outside .private, .venv and .git")
        clean.append(str(path))
    return sorted(set(clean))


def delivery(db, request_id):
    """The durable fact for one answered request, from the existing outbox and bindings."""
    from alfred_outcomes import delivered_owner_reply_ids
    row = db.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
    item = {"request_id": request_id, "request_state": row["status"] if row else "not_recorded"}
    keys = ["result:" + request_id]
    if row and row["status"] == "done":
        keys += delivered_owner_reply_ids(db, request_id, require_delivery=False)
    receipt = None
    for key in keys:
        receipt = db.execute("SELECT id,status,telegram_id FROM outbox WHERE id=?", (key,)).fetchone()
        if receipt:
            break
    binding = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_direct_returns'").fetchone():
        binding = db.execute("SELECT route_id,receipt FROM appserver_direct_returns WHERE request_id=?", (request_id,)).fetchone()
        if binding:
            item["direct_route_id"] = binding["route_id"]
    if receipt:
        parts = [list(part) for part in db.execute(
            "SELECT part_index,status,telegram_id FROM outbox_parts WHERE outbox_id=? ORDER BY part_index", (receipt["id"],))]
        item.update(outbox_id=receipt["id"], delivery_state=receipt["status"], telegram_id=receipt["telegram_id"], parts=parts)
    elif binding and binding["receipt"]:
        saved = json.loads(binding["receipt"])
        item.update(outbox_id="result:" + request_id, delivery_state=saved["status"], telegram_id=saved.get("telegram_id"),
                    parts=saved.get("parts", []), retained_receipt=True)
    else:
        item["delivery_state"] = "not_recorded"
    item["durable"] = (item["delivery_state"] == "suppressed" or (
        item["delivery_state"] == "sent" and type(item.get("telegram_id")) is int and item["telegram_id"] > 0
        and all(part[1] == "sent" for part in item.get("parts", []))))
    notices = [dict(n) for n in db.execute("SELECT id,status FROM outbox WHERE id LIKE ? AND status IN ('uncertain','failed')",
                                           ("result:" + request_id + ":%",))]
    if notices:
        item["notices_needing_reconciliation"] = notices
    return item


def private_file(path, data):
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    return path


def run_hooks(config, root, workspace, request_ids, paths):
    results, findings = [], []
    env = dict(os.environ, COS_WORKSPACE=str(workspace), CHIEF_CHECKPOINT_REQUESTS=json.dumps(request_ids),
               CHIEF_CHECKPOINT_PATHS=json.dumps(paths))
    for kind in ("export", "verify"):
        commands = config.get(kind, [])
        if not commands:
            results.append({"kind": kind, "status": "not_configured"})
        for argv in commands:
            item = {"kind": kind, "command": Path(argv[0]).name}
            try:
                # No time limit: an export finishes or fails on its own; the operator can stop it.
                done = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, stdin=subprocess.DEVNULL)
                output = (b"$ exit " + str(done.returncode).encode() + b"\n--- stdout\n" + done.stdout
                          + b"\n--- stderr\n" + done.stderr)
                item.update(status="passed" if done.returncode == 0 else "failed", exit_code=done.returncode,
                            output_sha256=hashlib.sha256(output).hexdigest())
                if done.returncode:
                    # Complete evidence stays private; the record carries a redacted excerpt.
                    folder = Path(root) / "checkpoints"
                    folder.mkdir(mode=0o700, exist_ok=True)
                    item["evidence_file"] = str(private_file(folder / ("hook-" + item["output_sha256"] + ".log"), output))
                    tail = (done.stdout + done.stderr).decode("utf-8", "replace").strip().splitlines()[-20:]
                    item["output_tail"] = redact("\n".join(tail), conservative=False)[0]
            except OSError as error:
                item.update(status="failed", error=type(error).__name__)
            if item["status"] != "passed":
                findings.append({"problem": kind + " command failed", "command": item["command"]})
            results.append(item)
    return results, findings


def build_tree(root, workspace, head, paths):
    """Stage only the declared paths into a private index: (tree, diff bytes, changes)."""
    with tempfile.TemporaryDirectory(dir=Path(root)) as directory:
        index = Path(directory) / "index"
        git(workspace, "read-tree", head, index=index)
        git(workspace, "add", "-A", "--", *paths, index=index)
        tree = out(workspace, "write-tree", index=index)
        diff = git(workspace, "diff", "--cached", "--binary", "--full-index", "--no-color", "--no-ext-diff",
                   "--no-textconv", "--no-renames", head, index=index).stdout
        changes = [line.split("\t", 1) for line in out(workspace, "diff", "--cached", "--name-status", "--no-renames",
                                                       head, index=index).splitlines() if line]
    return tree, diff, changes


def scan(diff):
    """Fresh redaction findings on added lines. Names and locations only, never the content."""
    findings, gaps, path, line_number = [], [], None, 0
    for raw in diff.split(b"\n"):
        if raw.startswith(b"diff --git "):
            path, line_number = raw.decode("utf-8", "replace").rsplit(" b/", 1)[-1], 0
        elif raw.startswith(b"GIT binary patch"):
            gaps.append({"path": path, "gap": "binary content is not scanned"})
        elif raw.startswith(b"@@"):
            match = re.search(rb"\+(\d+)", raw)
            line_number = int(match[1]) - 1 if match else 0
        elif raw.startswith(b"+") and not raw.startswith(b"+++ "):
            line_number += 1
            _, counts = redact(raw[1:].decode("utf-8", "replace"), conservative=False)
            names = sorted(name for name in counts if name not in ALLOWED)
            if names:
                findings.append({"problem": "credential-like content", "path": path, "line": line_number, "patterns": names})
        elif raw.startswith(b" "):
            line_number += 1
    return findings, gaps


def index_conflicts(workspace, head, tree, changes):
    """Changed paths whose real index entry is neither HEAD's nor the reviewed one: someone else's staged work."""
    conflicts = []
    for _, path in changes:
        staged = out(workspace, "ls-files", "-s", "--", path).split(" ")[1:2]
        before = out(workspace, "ls-tree", head, "--", path).split(" ")[2:3]
        after = out(workspace, "ls-tree", tree, "--", path).split(" ")[2:3]
        staged = staged[0] if staged else None
        before = before[0].split("\t")[0] if before else None
        after = after[0].split("\t")[0] if after else None
        if staged not in {before, after}:
            conflicts.append({"problem": "another staged version exists in the operator's index", "path": path})
    return conflicts


def target_problem(workspace, record):
    """The remote must still resolve to exactly the reviewed fetch and push URLs (after insteadOf rewriting)."""
    fetch = git(workspace, "remote", "get-url", "--all", record["remote"], check=False)
    push = git(workspace, "remote", "get-url", "--push", "--all", record["remote"], check=False)
    if fetch.returncode or fetch.stdout.decode().split() != [record["fetch_url"]]:
        return {"problem": "the remote fetch URL is not the expected one; nothing was contacted"}
    if push.returncode or push.stdout.decode().split() != [record["push_url"]]:
        return {"problem": "the remote push URL is not the expected one; nothing was pushed"}
    return None


def remote_tip(workspace, remote, branch):
    lines = out(workspace, "ls-remote", "--heads", remote, "refs/heads/" + branch).splitlines()
    return lines[0].split("\t")[0] if lines else None


def save(db, digest, state, record):
    now = time.time()
    with db:
        db.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(digest) DO UPDATE SET state=excluded.state,"
                   "record=excluded.record,updated=excluded.updated", (digest, state, json.dumps(record, sort_keys=True), now, now))


def prepare(inbox, request_ids, paths=None, message=None, workspace=WORKSPACE):
    db, root, workspace = inbox.db, Path(inbox.root), Path(workspace)
    ensure_table(db)
    config = load_config(root)
    if not request_ids:
        raise BridgeError("Name the answered request IDs in this batch")
    paths = declared(config.get("paths", []) if paths is None else paths)
    record = {"requests": sorted(set(request_ids)), "paths": paths,
              "message": message or "Chief checkpoint: " + str(len(set(request_ids))) + " answered assignment(s)",
              "coverage_gaps": [{"gap": gap, "source": "known_gaps"} for gap in config.get("known_gaps", [])]}
    findings = []
    record["deliveries"] = [delivery(db, request_id) for request_id in record["requests"]]
    for item in record["deliveries"]:
        if not item["durable"]:
            findings.append({"problem": "delivery is not durable: " + item["delivery_state"], "request_id": item["request_id"]})
        if item.get("notices_needing_reconciliation"):
            findings.append({"problem": "a notice needs reconciliation", "request_id": item["request_id"]})
    record["hooks"] = []
    if paths and not findings:
        record["hooks"], failures = run_hooks(config, root, workspace, record["requests"], paths)
        findings += failures
    record["coverage_gaps"] += [{"gap": hook["kind"] + " not configured"} for hook in record["hooks"] if hook["status"] == "not_configured"]
    diff = b""
    if paths and not findings:
        if Path(out(workspace, "rev-parse", "--show-toplevel")).resolve() != workspace.resolve():
            raise BridgeError("The workspace must be the root of its private Git repository")
        record["head"] = out(workspace, "rev-parse", "--verify", "HEAD")
        record["branch"] = out(workspace, "symbolic-ref", "--short", "HEAD")
        record["tree"], diff, record["changes"] = build_tree(root, workspace, record["head"], paths)
        record["diff_sha256"] = hashlib.sha256(diff).hexdigest()
        secrets, gaps = scan(diff)
        findings += secrets + index_conflicts(workspace, record["head"], record["tree"], record["changes"])
        record["coverage_gaps"] += gaps
        record["remote"] = config.get("remote") or None
        record["push"] = bool(config.get("push"))
        if record["remote"]:
            record.update(fetch_url=config["fetch_url"], push_url=config["push_url"])
            problem = target_problem(workspace, record)
            if problem:
                findings.append(problem)
            else:
                record["remote_tip"] = remote_tip(workspace, record["remote"], record["branch"])
                if record["push"] and record["remote_tip"] != record["head"]:
                    # Pushing the checkpoint would also publish unreviewed local or missing history.
                    findings.append({"problem": "the remote branch is not at the reviewed HEAD; reconcile it before checkpointing"})
                elif record["remote_tip"] not in {None, record["head"]} and git(
                        workspace, "merge-base", "--is-ancestor", record["remote_tip"], record["head"], check=False).returncode:
                    findings.append({"problem": "the remote branch diverged; reconcile it before checkpointing"})
    record["kind"] = "commit" if record.get("changes") else "delivery_receipt"
    record["findings"] = findings
    core = {key: value for key, value in record.items() if key not in {"findings"}}
    digest = hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()
    state = "blocked" if findings else "prepared"
    folder = root / "checkpoints"
    folder.mkdir(mode=0o700, exist_ok=True)
    diff_file = folder / (digest + ".diff")
    if not diff_file.exists():
        descriptor = os.open(diff_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(diff)
    previous = db.execute("SELECT state FROM checkpoints WHERE digest=?", (digest,)).fetchone()
    if previous is None or previous["state"] in {"prepared", "blocked"}:
        save(db, digest, state, record)
    else:
        state = previous["state"]
    return dict(record, digest=digest, state=state, diff_file=str(diff_file),
                next="Review diff_file, then run confirm with this digest." if state == "prepared"
                else "Resolve the findings, then prepare again." if state == "blocked" else "Already " + state + "; confirm resumes it.")


def confirm(inbox, digest, workspace=WORKSPACE):
    # A second process may resume the same receipt while the first is still pushing.
    # Serialize finalizers only. Telegram intake and outbox delivery stay independent.
    lock_path = Path(inbox.root) / "checkpoint-commit.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _confirm(inbox, digest, workspace)


def _confirm(inbox, digest, workspace):
    db, workspace = inbox.db, Path(workspace)
    ensure_table(db)
    row = db.execute("SELECT * FROM checkpoints WHERE digest=?", (digest,)).fetchone()
    if row is None:
        raise BridgeError("No prepared checkpoint has this digest")
    record, state = json.loads(row["record"]), row["state"]
    push = record.get("push", False)  # Fixed at review; never chosen at confirm time.
    if state == "blocked":
        raise BridgeError("This checkpoint has findings; resolve them and prepare again")
    if state == "receipted" or state == "pushed" or (state == "committed" and not push):
        return dict(record, digest=digest, state=state)  # Already complete: nothing is repeated.
    problems = []
    if state == "prepared":
        # Delivery receipts only move forward; a lost or changed receipt is a fresh fact.
        for before in record["deliveries"]:
            now = delivery(db, before["request_id"])
            if not now["durable"] and not (now["delivery_state"] == "not_recorded" and before["durable"]):
                problems.append({"problem": "delivery is no longer durable: " + now["delivery_state"], "request_id": before["request_id"]})
    if record["kind"] == "delivery_receipt":
        if problems:
            return dict(record, digest=digest, state=state, findings=problems)
        record["receipted_at"] = time.time()
        save(db, digest, "receipted", record)
        return dict(record, digest=digest, state="receipted")
    branch_ref = "refs/heads/" + record["branch"]
    head = out(workspace, "rev-parse", "--verify", "HEAD")
    if state == "prepared":
        if head != record["head"] or out(workspace, "symbolic-ref", "--short", "HEAD") != record["branch"]:
            problems.append({"problem": "HEAD moved since review; prepare again"})
        else:
            tree, diff, changes = build_tree(inbox.root, workspace, record["head"], record["paths"])
            if tree != record["tree"] or hashlib.sha256(diff).hexdigest() != record["diff_sha256"]:
                problems.append({"problem": "declared files changed since review; prepare again"})
            else:
                secrets, _ = scan(diff)
                problems += secrets + index_conflicts(workspace, record["head"], tree, changes)
        if not problems and push:
            problem = target_problem(workspace, record)
            if problem or remote_tip(workspace, record["remote"], record["branch"]) != record["remote_tip"]:
                problems.append(problem or {"problem": "the remote branch changed since review; prepare again"})
        if problems:
            return dict(record, digest=digest, state=state, findings=problems)
        record["commit"] = out(workspace, "commit-tree", record["tree"], "-p", record["head"], "-m",
                               record["message"] + "\n\nCheckpoint: " + digest)
        state = "committing"
        save(db, digest, state, record)
    if state in {"committing", "committed", "pushing"} and (
            out(workspace, "rev-parse", record["commit"] + "^{tree}") != record["tree"]
            or out(workspace, "rev-parse", record["commit"] + "^") != record["head"]):
        return dict(record, digest=digest, state=state, findings=[{"problem": "the saved commit is not the reviewed one; inspect it"}])
    if state == "committing":
        if head == record["head"]:
            git(workspace, "update-ref", "-m", "chief checkpoint", branch_ref, record["commit"], record["head"])
        elif head != record["commit"]:
            return dict(record, digest=digest, state=state,
                        findings=[{"problem": "HEAD moved during the checkpoint; inspect " + record["commit"] + " before retrying"}])
        # Point only the changed entries of the operator's index at the committed blobs.
        lines = []
        for _, path in record["changes"]:
            entry = out(workspace, "ls-tree", record["commit"], "--", path)
            lines.append(entry.split("\t")[0] + "\t" + path if entry else "0 " + EMPTY + "\t" + path)
        git(workspace, "update-index", "--index-info", data=("\n".join(lines) + "\n").encode())
        state = "committed"
        save(db, digest, state, record)
    if push and state in {"committed", "pushing"}:
        problem = target_problem(workspace, record)
        if problem:
            return dict(record, digest=digest, state=state, findings=[problem])
        tip = remote_tip(workspace, record["remote"], record["branch"])
        if tip != record["commit"]:
            if tip != record.get("remote_tip"):
                return dict(record, digest=digest, state=state,
                            findings=[{"problem": "the remote branch changed since review; reconcile before pushing"}])
            state = "pushing"
            save(db, digest, state, record)
            # Only the reviewed commit, whose parent is already the remote tip; no tags, no force.
            git(workspace, "-c", "push.followTags=false", "push", "--no-follow-tags", "--porcelain",
                record["remote"], record["commit"] + ":" + branch_ref)
            if remote_tip(workspace, record["remote"], record["branch"]) != record["commit"]:
                return dict(record, digest=digest, state=state, findings=[{"problem": "push not confirmed by the remote"}])
        record["pushed_at"] = time.time()
        state = "pushed"
        save(db, digest, state, record)
    return dict(record, digest=digest, state=state)


def status(inbox, digest):
    ensure_table(inbox.db)
    row = inbox.db.execute("SELECT * FROM checkpoints WHERE digest=?", (digest,)).fetchone()
    if row is None:
        raise BridgeError("No prepared checkpoint has this digest")
    return dict(json.loads(row["record"]), digest=digest, state=row["state"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("prepare", "confirm", "status"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--request-id", action="append", default=[], help="An answered request in this batch (repeat)")
    parser.add_argument("--path", action="append", help="A workspace path to checkpoint (repeat); default: checkpoint.json paths")
    parser.add_argument("--message")
    parser.add_argument("--digest")
    args = parser.parse_args()
    from alfred_local import private_root
    try:
        private_root(args.root)
        inbox = Inbox(args.root)
    except (BridgeError, OSError):
        print(json.dumps({"error": "Private bridge storage unavailable"}))
        return 1
    try:
        if args.command == "prepare":
            output = prepare(inbox, args.request_id, args.path, args.message)
        elif args.command == "confirm":
            output = confirm(inbox, args.digest)
        else:
            output = status(inbox, args.digest)
    except (BridgeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error) if isinstance(error, BridgeError) else type(error).__name__}))
        return 1
    finally:
        inbox.db.close()
    print(json.dumps(output, ensure_ascii=False))
    complete = {"prepare": {"prepared"}, "confirm": {"committed", "pushed", "receipted"}}.get(args.command)
    return 0 if complete is None or output["state"] in complete and not output.get("findings") else 3


if __name__ == "__main__":
    raise SystemExit(main())
