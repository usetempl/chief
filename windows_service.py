"""One Windows logon task, running the existing Unix collector inside WSL2."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def check_workspace(workspace):
    if os.environ.get("WSL_DISTRO_NAME"):
        # Windows-mounted folders do not provide this runtime's Unix privacy guarantees.
        resolved = Path(workspace).resolve()
        if not resolved.is_relative_to(Path.home().resolve()) or resolved.is_relative_to(Path("/mnt")):
            raise ValueError("Keep the WSL workspace inside your Linux home, not a Windows drive")


def invoke(args, check=True):
    result = subprocess.run(args, capture_output=True, timeout=30)
    if check and result.returncode:
        raise ValueError("Windows task operation failed; inspect Task Scheduler in your own account")
    return result


def decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\0" in data[:200]:
        return data.decode("utf-16-le")
    return data.decode("utf-8-sig")


def windows_path(path):
    return decode(invoke(["wslpath", "-w", str(path)]).stdout).strip()


def task_xml(workspace, distro, linux_user, windows_sid, executable):
    ET.register_namespace("", NS)
    root = ET.Element("{" + NS + "}Task", {"version": "1.2"})
    def node(parent, name, value=None, **attrs):
        result = ET.SubElement(parent, "{" + NS + "}" + name, attrs)
        result.text = value
        return result
    node(node(root, "RegistrationInfo"), "Description", "Chief: " + str(workspace))
    trigger = node(node(root, "Triggers"), "LogonTrigger")
    node(trigger, "Enabled", "true")
    node(trigger, "UserId", windows_sid)
    principal = node(node(root, "Principals"), "Principal", id="Owner")
    node(principal, "UserId", windows_sid)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(root, "Settings")
    for name, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                        "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true",
                        "ExecutionTimeLimit": "PT0S", "Enabled": "false"}.items():
        node(settings, name, value)
    restart = node(settings, "RestartOnFailure")
    node(restart, "Interval", "PT1M")
    node(restart, "Count", "3")
    action = node(node(root, "Actions", Context="Owner"), "Exec")
    node(action, "Command", executable)
    # No shell. Windows argument quoting keeps spaces and metacharacters as data.
    node(action, "Arguments", subprocess.list2cmdline([
        "--distribution", distro, "--user", linux_user, "--exec", str(workspace / ".venv/bin/python"),
        str(workspace / "setup.py"), "run-service", "--workspace", str(workspace)]))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def enabled_xml(xml):
    root = ET.fromstring(xml)
    root.find("{" + NS + "}Settings/{" + NS + "}Enabled").text = "true"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def task_matches(actual, expected):
    """Compare actions, privileges, trigger and explicit settings; allow OS-added defaults."""
    def normalized(element):
        return (element.tag, element.attrib, (element.text or "").strip(),
                tuple(normalized(child) for child in element))
    try:
        current, desired = ET.fromstring(actual), ET.fromstring(expected)
        prefix = "{" + NS + "}"
        if current.tag != desired.tag:
            return False
        for name in ("Triggers", "Principals", "Actions"):
            left, right = current.find(prefix + name), desired.find(prefix + name)
            if left is None or normalized(left) != normalized(right):
                return False
        for setting in desired.find(prefix + "Settings"):
            value = current.find(prefix + "Settings/" + setting.tag)
            if value is None or normalized(value) != normalized(setting):
                return False
        return True
    except (ET.ParseError, TypeError, ValueError):
        return False


def install(workspace):
    from setup import private_write
    from alfred_appserver import load_config
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    if not distro or "wsl2" not in platform.release().lower() or os.getuid() == 0:
        raise ValueError("Use your own non-root account in WSL2; service installation did not run")
    check_workspace(workspace)
    if not load_config(workspace / ".private") or not (workspace / ".venv/bin/python").is_file():
        raise ValueError("Create the virtual environment and run connect first")
    binaries = {name: shutil.which(name) for name in ("codex", "node", "wsl.exe", "schtasks.exe", "whoami.exe")}
    if any(not binaries[name] for name in ("codex", "wsl.exe", "schtasks.exe", "whoami.exe")):
        raise ValueError("Codex and normal WSL Windows interoperability must be available")
    identity = invoke([binaries["whoami.exe"], "/user", "/fo", "csv", "/nh"]).stdout
    # SID is ASCII even when the account name uses the Windows console code page.
    sids = re.findall(rb"\bS-1-(?:\d+-)+\d+\b", identity.replace(b"\0", b""))
    if len(sids) != 1:
        raise ValueError("Could not verify your Windows user identity")
    label = "ChiefOfShipping-" + hashlib.sha256((distro + "\0" + str(workspace)).encode()).hexdigest()[:12]
    xml = task_xml(workspace, distro, pwd.getpwuid(os.getuid()).pw_name, sids[0].decode(), windows_path(binaries["wsl.exe"]))
    active_xml = enabled_xml(xml)
    path_parts = [str(Path(binaries[name]).parent) for name in ("codex", "node") if binaries[name]]
    env = {"PATH": ":".join(dict.fromkeys(path_parts + ["/usr/local/bin", "/usr/bin", "/bin"]))}
    config = workspace / ".private/windows-service.json"
    data = json.dumps({"task_name": label, "env": env}, sort_keys=True) + "\n"
    with (workspace / ".private/service-setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if config.exists() or config.is_symlink():
            if config.is_symlink() or config.read_text() != data:
                raise ValueError("An existing service configuration differs; it was preserved")
        else:
            private_write(config, data)
        receipt = workspace / ".private/windows-task.xml"
        if receipt.exists() or receipt.is_symlink():
            if receipt.is_symlink() or receipt.read_bytes() != xml:
                raise ValueError("An existing Windows task record differs; it was preserved")
        else:
            private_write(receipt, xml.decode())
        existing = invoke([binaries["schtasks.exe"], "/Query", "/TN", label, "/XML"], check=False)
        if existing.returncode == 0:
            if not any(task_matches(decode(existing.stdout), candidate) for candidate in (xml, active_xml)):
                raise ValueError("A Windows task with this name differs; it was preserved")
        else:
            # No /F: a query/access race must never overwrite somebody else's task.
            invoke([binaries["schtasks.exe"], "/Create", "/TN", label, "/XML", windows_path(receipt)])
        confirmed = invoke([binaries["schtasks.exe"], "/Query", "/TN", label, "/XML"])
        if task_matches(decode(confirmed.stdout), xml):
            # Registration is disabled until its exact command and privileges are verified.
            invoke([binaries["schtasks.exe"], "/Change", "/TN", label, "/ENABLE"])
            confirmed = invoke([binaries["schtasks.exe"], "/Query", "/TN", label, "/XML"])
        if not task_matches(decode(confirmed.stdout), active_xml):
            raise ValueError("Windows task " + label + " is registered and needs inspection; setup did not request a start")
        invoke([binaries["schtasks.exe"], "/Run", "/TN", label])
    print("Configured one Windows logon task: " + label + ". Verify real Telegram delivery before relying on it.")


def forward_hangup():
    if os.environ.get("WSL_DISTRO_NAME") and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: os.kill(os.getpid(), signal.SIGTERM))


def run(workspace):
    # Restore only the recorded executable search path, never credentials or shell code.
    from setup import use_workspace
    use_workspace(workspace)
    config = workspace / ".private/windows-service.json"
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The Windows service record must be an owner-only regular file")
    env = json.loads(config.read_text())["env"]
    if set(env) != {"PATH"} or not isinstance(env["PATH"], str):
        raise ValueError("Unexpected Windows service environment")
    os.environ["PATH"] = env["PATH"]
    os.chdir(workspace)
    for descriptor, name in ((1, "service.log"), (2, "service-error.log")):
        fd = os.open(workspace / ".private" / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.dup2(fd, descriptor)
        os.close(fd)
    os.execv(sys.executable, [sys.executable, str(workspace / "setup.py"), "run", "--workspace", str(workspace)])
