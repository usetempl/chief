#!/usr/bin/env python3
"""Prepare and run an independent, local Codex Telegram coordinator."""
import argparse
import asyncio
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import uuid

PACKAGE = Path(__file__).resolve().parent
PACKAGE_FILES = {
    "setup.py", "windows_service.py", "onboard.py", "snapshot.py", "requirements.txt", "README.md", "docs/telegram.md", "VERSION",
    *{"runtime/" + name + ".py" for name in (
        "operator_policy", "telegram_bridge", "alfred_appserver", "alfred_audio", "alfred_projects", "alfred_local", "alfred_maintenance",
        "alfred_report", "alfred_outcomes", "alfred_entry", "alfred_tasks", "alfred_board", "history_safety")},
}


def private_write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def prepare(destination, owner_label, bot_username, history, nickname=None):
    from onboard import create_workspace, validate_nickname
    from windows_service import check_workspace
    check_workspace(Path(destination))
    nickname = validate_nickname(nickname)
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username) or not bot_username.lower().endswith("bot"):
        raise ValueError("Use the exact new bot username, without @")
    if not owner_label.strip() or len(owner_label) > 80 or any(ord(c) < 32 for c in owner_label):
        raise ValueError("Use a short owner display name")
    manifest = json.loads((PACKAGE / "MANIFEST.json").read_text())
    if set(manifest["files"]) != PACKAGE_FILES:
        raise ValueError("Package manifest differs from the supported source list")
    for name, digest in manifest["files"].items():
        path = PACKAGE / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != path.parent.resolve():
            raise ValueError("Package contains an invalid source path")
        if (PACKAGE / "runtime").is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Package integrity check failed")
    workspace = create_workspace(destination, "codex", history, nickname)
    (workspace / "runtime").mkdir(mode=0o700)
    for name in sorted(PACKAGE_FILES | {"MANIFEST.json"}):
        (workspace / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / name, workspace / name)
    (workspace / ".private").mkdir(mode=0o700)
    private_write(workspace / ".private/profile.json", json.dumps({
        "owner_label": owner_label.strip(), "bot_username": bot_username, "nickname": nickname,
    }) + "\n")
    private_write(workspace / ".private/maintenance.json", json.dumps({
        "enabled": False, "interval_days": 7, "timezone": None,
    }) + "\n")
    with (workspace / ".gitignore").open("a") as stream:
        stream.write(".private/\n.venv/\n__pycache__/\n*.pyc\n*.log\n")
    with (workspace / "COORDINATOR.md").open("a") as stream:
        stream.write("\n## Telegram runtime\n\nThis workspace uses one persistent Codex app-server thread. "
                     "setup.py creates it once, without a model turn. Do not create another coordinator. "
                     "Only scoped replies and the maintenance notice guard queue Telegram messages; final text is not sent. "
                     "For an older thread without the notice tool, runtime/alfred_maintenance.py notice and notice-status "
                     "use that same guard and receipt. Follow docs/telegram.md and require sent=true before authorized downtime. "
                     "Use /report for a saved report with no model call. Keep credentials and queues in .private, outside Git. "
                     "Read registered projects from projects.md. Existing owners appear as five-column table rows: "
                     "Priority, Project, Repository, Local checkout, Existing lead. Use priorities P1, P2, etc. "
                     "Only user-approved existing owner IDs go in that table. An ID alone does not prove the writer is available. "
                     "Native-held owners must release their writer before the app-server can resume them. "
                     "The initial bridge supports Codex owners; an unavailable Claude route must be reported, not replaced. "
                     "Weekly maintenance is disabled by default; enable it only at the user’s request. Healthy checks use no model. "
                     "Scheduled observations have source=maintenance and grant no new human or release authority. "
                     "Read docs/telegram.md for the checks, idle dispatch and activation limits.\n")
    return workspace


def use_workspace(path):
    workspace = Path(path).resolve(strict=True)
    private = workspace / ".private"
    info = private.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Use the prepared workspace and its owner-only .private directory")
    os.environ["COS_WORKSPACE"] = str(workspace)
    sys.path.insert(0, str(workspace / "runtime"))
    return workspace


class RpcRejected(ValueError):
    """The server explicitly rejected an operation, so it was not accepted."""


async def rpc(ws, method, params):
    identifier = str(uuid.uuid4())
    await ws.send(json.dumps({"id": identifier, "method": method, "params": params}))
    async with asyncio.timeout(30):
        async for raw in ws:
            message = json.loads(raw)
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise RpcRejected("Codex rejected " + method + "; inspect its native sign-in and configuration")
                return message["result"]
    raise ValueError("Codex disconnected before confirming " + method)


async def create_thread(workspace):
    from websockets.asyncio.client import unix_connect
    from alfred_appserver import DEFAULT_SOCKET, TELEGRAM_REPLY_TOOL, MAINTENANCE_NOTICE_TOOL, verified_socket_path
    from alfred_projects import project_route_tool
    directory = workspace / ".private"
    config = directory / "appserver.json"
    if config.exists():
        return json.loads(config.read_text())["thread_id"]
    marker = directory / "thread-starting.json"
    if marker.exists():
        raise ValueError("A previous thread creation is uncertain. Inspect its native thread before retrying; do not create another")
    async with unix_connect(verified_socket_path(DEFAULT_SOCKET), max_size=4 * 1024 * 1024) as ws:
        await rpc(ws, "initialize", {"clientInfo": {"name": "chief_of_shipping_setup", "version": (workspace / "VERSION").read_text().strip()},
                                     "capabilities": {"experimentalApi": True}})
        await ws.send(json.dumps({"method": "initialized", "params": {}}))
        route = project_route_tool(workspace / "projects.md")
        # The runtime verifies every requested owner against the current private board.
        # The catalog must also permit owners accepted during later onboarding.
        route["inputSchema"]["properties"]["owner_id"].pop("enum", None)
        route["description"] = ("Send one concise brief to an existing owner registered in projects.md. "
                                "Read that file for current owners. Preserve sender authority and release rules. "
                                "Never create a replacement owner. A held owner keeps a pending brief.")
        private_write(marker, json.dumps({"cwd": str(workspace), "state": "creation_started"}) + "\n")
        try:
            result = await rpc(ws, "thread/start", {
                "cwd": str(workspace), "ephemeral": False, "serviceTier": "default",
                "dynamicTools": [TELEGRAM_REPLY_TOOL, route, MAINTENANCE_NOTICE_TOOL],
            })
        except RpcRejected:
            marker.unlink()  # Definite rejection only; ambiguous failures remain held.
            raise
        thread = result.get("thread", {})
        identifier = str(uuid.UUID(thread["id"]))
        private_write(config, json.dumps({"enabled": True, "thread_id": identifier,
                                          "socket_path": str(DEFAULT_SOCKET)}) + "\n")
        marker.unlink()
        return identifier


def connect(workspace):
    from operator_policy import BOT, OWNER_LABEL, ROOT
    from telegram_bridge import Inbox, Telegram
    lock = open(ROOT / "setup.lock", "a")
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binary = shutil.which("codex")
        if binary is None:
            raise ValueError("Install Codex CLI and complete its normal login first")
        token_path = ROOT / "bot-token"
        token = None
        if not token_path.exists():
            token = getpass.getpass("New Telegram bot token (hidden; never paste into chat): ").strip()
            if not re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{30,64}", token):
                raise ValueError("Invalid bot token format; nothing was saved")
        api = Telegram(ROOT, token=token)
        if api.call("getMe").get("username") != BOT:
            raise ValueError("Bot identity differs from the prepared profile; no thread was created")
        if api.call("getWebhookInfo").get("url"):
            raise ValueError("This bot already has a webhook. Use a new bot; no webhook was changed")
        if token is not None:
            private_write(token_path, token + "\n")
        subprocess.run([binary, "app-server", "daemon", "start"], check=True, timeout=30, capture_output=True)
        identifier = asyncio.run(create_thread(workspace))
        inbox = Inbox(ROOT)
        try:
            if not inbox.db.execute("SELECT 1 FROM users WHERE label=?", (OWNER_LABEL,)).fetchone():
                print("Private enrollment invitation: " + inbox.invitation(OWNER_LABEL))
        finally:
            inbox.db.close()
        print("Coordinator prepared: " + identifier + ". No model turn was started.")


def service(workspace):
    """Install this workspace's one collector, without touching another service."""
    if sys.platform == "linux":
        from windows_service import install
        return install(workspace)
    if sys.platform != "darwin":
        raise ValueError("Service setup supports macOS or Windows through WSL2")
    from alfred_appserver import load_config
    if not load_config(workspace / ".private"):
        raise ValueError("Run connect before installing the service")
    python = workspace / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Create this workspace's .venv and install requirements first")
    label = "org.chief-of-shipping." + hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
    path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = shutil.which("codex")
    if not binary:
        raise ValueError("Codex CLI must be available on PATH")
    data = {"Label": label, "ProgramArguments": [str(python), str(workspace / "setup.py"), "run", "--workspace", str(workspace)],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "WorkingDirectory": str(workspace),
            "EnvironmentVariables": {"PATH": str(Path(binary).parent) + ":/usr/local/bin:/usr/bin:/bin"},
            "StandardOutPath": str(workspace / ".private/service.log"),
            "StandardErrorPath": str(workspace / ".private/service-error.log")}
    if path.exists() or path.is_symlink():
        if path.is_symlink() or plistlib.loads(path.read_bytes()) != data:
            raise ValueError("An existing service configuration differs; it was preserved")
        loaded = subprocess.run(["launchctl", "print", "gui/" + str(os.getuid()) + "/" + label],
                                timeout=10, capture_output=True)
        if loaded.returncode == 0:
            print("Collector already installed: " + label)
            return
    else:
        private_write(path, plistlib.dumps(data).decode())
    # A failed bootstrap can be retried using this same verified plist and label.
    # Never delete an uncertain service or start a different collector to recover.
    subprocess.run(["launchctl", "bootstrap", "gui/" + str(os.getuid()), str(path)], check=True, timeout=30, capture_output=True)
    print("Installed one collector: " + label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--directory", required=True, type=Path)
    prep.add_argument("--owner-label", required=True)
    prep.add_argument("--bot-username", required=True)
    prep.add_argument("--nickname", help="Optional name for this Chief")
    prep.add_argument("--history", choices=("yes", "no"), required=True)
    for name in ("connect", "run", "service", "run-service", "doctor", "bot-profile"):
        sub.add_parser(name).add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    try:
        os.umask(0o077)
        if args.command == "prepare":
            print("Prepared: " + str(prepare(args.directory, args.owner_label, args.bot_username, args.history == "yes", args.nickname)))
            return 0
        if args.command == "run-service":
            from windows_service import run
            return run(args.workspace.resolve(strict=True))
        workspace = use_workspace(args.workspace)
        if args.command == "connect":
            connect(workspace)
        elif args.command == "bot-profile":
            from telegram_bridge import Inbox, Telegram, configure_presentation
            inbox = Inbox(workspace / ".private")
            try:
                print(json.dumps(configure_presentation(Telegram(inbox.root), inbox)))
            finally:
                inbox.db.close()
        elif args.command == "service":
            service(workspace)
        elif args.command == "run":
            from telegram_bridge import Inbox, serve
            from alfred_appserver import load_config
            from windows_service import forward_hangup
            if not load_config(workspace / ".private"):
                raise ValueError("Run connect first")
            inbox = Inbox(workspace / ".private")
            try:
                forward_hangup()
                serve(inbox)
            finally:
                inbox.db.close()
        else:
            from alfred_appserver import load_config
            from operator_policy import BOT
            print(json.dumps({"workspace": str(workspace), "bot_username": BOT,
                              "coordinator_configured": load_config(workspace / ".private") is not None,
                              "credential_present": (workspace / ".private/bot-token").is_file(),
                              "live_delivery": "Verify from the enrolled Telegram account; configuration alone is not delivery"}))
    except Exception as error:
        # Never print provider exception bodies, process output or credentials.
        print("Setup stopped: " + (str(error) if type(error) in (ValueError, RpcRejected) else type(error).__name__) + ".", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
