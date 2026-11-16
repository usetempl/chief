"""Opt-in, bounded on-device transcription. No network or model download path."""
from contextlib import suppress
import array
import fcntl
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import wave

MAX_SECONDS = 300
MAX_BYTES = 20 * 1024 * 1024
EXTENSIONS = {".ogg", ".mp3", ".m4a", ".wav", ".oga", ".opus", ".flac", ".aac", ".mp4", ".webm"}


def unavailable(reason):
    return {"status": "unavailable", "reason": reason}


def private_file(path, limit):
    """Open without following a final symlink, then check the opened object."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= limit):
            raise ValueError("unsafe_file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return stream.read(limit + 1)
    finally:
        if fd is not None:
            os.close(fd)


def recording(root, request_id, attachment):
    path = Path(attachment["local_path"])
    directory = (Path(root) / "attachments").resolve()
    if (attachment["kind"] not in {"voice", "audio"} or attachment["status"] != "ready"
            or not path.is_absolute() or path.parent != directory or path.suffix not in EXTENSIONS
            or path.stem != hashlib.sha256(request_id.encode()).hexdigest()):
        raise ValueError("unsafe_attachment")
    data = private_file(path, MAX_BYTES)
    if len(data) != attachment["size"]:
        raise ValueError("changed_attachment")
    return data


@lru_cache(maxsize=2)
def verified_model(path, size, mtime, expected):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == expected


def load_config(root):
    path = Path(root) / "local-audio.json"
    if not path.exists():
        return None
    config = json.loads(private_file(path, 4096))
    if config == {"enabled": False}:
        return None
    if (set(config) != {"enabled", "whisper", "ffmpeg", "model", "model_sha256"}
            or config["enabled"] is not True
            or not re.fullmatch(r"[0-9a-f]{64}", config["model_sha256"])):
        raise ValueError("invalid_config")
    for name in ("whisper", "ffmpeg", "model"):
        p = Path(config[name])
        info = p.stat()
        if (not p.is_absolute() or not stat.S_ISREG(info.st_mode)
                or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022
                or (name != "model" and not os.access(p, os.X_OK))):
            raise ValueError("invalid_config_path")
    p = Path(config["model"])
    info = p.stat()
    if not verified_model(str(p), info.st_size, info.st_mtime_ns, config["model_sha256"]):
        raise ValueError("model_digest_mismatch")
    return config


def supervise(args, parent_pid, timeout):
    """Independent deadline/parent-death watcher; retains the inherited decode lock."""
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = subprocess.Popen(args, stdin=sys.stdin.buffer, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout
    while child.poll() is None:
        if stopping or os.getppid() != parent_pid or time.monotonic() >= deadline:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            return 124
        time.sleep(0.05)
    return child.returncode


def run_local(args, cwd, timeout, *, input_data=None, cancel=None, lock_fd=None):
    # A private, seekable descriptor supports trailing MP4 metadata without exposing
    # a file protocol or filename to the demuxer. TemporaryFile is unlinked on Unix.
    with tempfile.TemporaryFile(dir=cwd) as source:
        if input_data is not None:
            source.write(input_data)
            source.seek(0)
        _run_local(args, cwd, timeout, source, cancel=cancel, lock_fd=lock_fd)


def _run_local(args, cwd, timeout, source, *, cancel=None, lock_fd=None):
    # argv only. No shell, inherited credentials, external URL input or raw logs.
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--supervise",
        str(os.getpid()), str(timeout), *args], cwd=cwd,
        stdin=source,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
        env={"PATH": os.defpath, "LANG": "en_US.UTF-8", "TMPDIR": str(cwd)})
    try:
        deadline = time.monotonic() + timeout + 3
        while True:
            if cancel is not None and cancel.is_set():
                raise ValueError("decode_cancelled")
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode == 124:
            raise subprocess.TimeoutExpired(args, timeout)
        if process.returncode != 0:
            raise ValueError("decoder_failed")
    except BaseException:
        if process.poll() is None:
            process.terminate()  # Watcher kills/reaps its child before releasing the lock.
            process.wait(timeout=3)
        raise


def parse_result(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid_output")
    segments = payload.get("transcription")
    if not isinstance(segments, list) or not segments:
        raise ValueError("no_speech")
    if any(not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
           or not isinstance(segment.get("tokens", []), list)
           or any(not isinstance(token, dict) for token in segment.get("tokens", [])) for segment in segments):
        raise ValueError("invalid_output")
    text = " ".join(segment["text"].strip() for segment in segments).strip()
    if not text or not any(c.isalnum() for c in text):
        raise ValueError("unclear_audio")
    for segment in segments:
        if re.search(r"\[(?:BLANK_AUDIO|NO_SPEECH|MUSIC|SILENCE)\]", segment["text"], re.I):
            raise ValueError("no_speech")
        probabilities = [token["p"] for token in segment.get("tokens", [])
                         if isinstance(token.get("text"), str) and token["text"].strip()
                         and not token["text"].startswith("[_")]
        if (not probabilities or any(not isinstance(p, (int, float)) or not math.isfinite(p)
                                     or not 0 <= p <= 1 for p in probabilities)
                or sum(probabilities) / len(probabilities) < 0.55
                or any(sum(probabilities[i:i+4])/4 < 0.25 for i in range(len(probabilities)-3))):
            raise ValueError("unclear_audio")
    result = payload.get("result")
    language = result.get("language") if isinstance(result, dict) else None
    return text, language if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}", language) else "unknown"


def transcribe(root, request_id, attachment, cancel=None):
    """Return untrusted transcript data, never execute its instructions."""
    started = time.monotonic()
    try:
        config = load_config(root)
        if config is None:
            return unavailable("local_decoder_not_enabled")
        data = recording(root, request_id, attachment)
        work = Path(root) / "audio-work"
        work.mkdir(mode=0o700, exist_ok=True)
        info = work.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_work_directory")
        lock_fd = os.open(work / "decoder.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "r+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return unavailable("local_decoder_busy")
            with tempfile.TemporaryDirectory(prefix="decode-", dir=work) as temp:
                temp = Path(temp)
                wav = temp / "audio.wav"
                run_local([config["ffmpeg"], "-nostdin", "-v", "error", "-protocol_whitelist", "fd",
                    "-format_whitelist", "ogg,mp3,wav,flac,aac,mov,matroska,webm", "-fd", "0", "-i", "fd:",
                    "-t", str(MAX_SECONDS + 1), "-fs", "10000000", "-map", "0:a:0",
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], temp, 30,
                    input_data=data, cancel=cancel, lock_fd=lock.fileno())
                with wave.open(str(wav), "rb") as stream:
                    duration = stream.getnframes() / stream.getframerate()
                    if not 0.2 <= duration <= MAX_SECONDS:
                        raise ValueError("audio_duration_limit")
                    samples = array.array("h", stream.readframes(stream.getnframes()))
                if not samples or max(abs(n) for n in samples) < 400 or math.sqrt(sum(n*n for n in samples)/len(samples)) < 70:
                    raise ValueError("no_speech")
                output = temp / "result"
                run_local([config["whisper"], "-m", config["model"], "-f", str(wav), "-l", "auto",
                    "-t", "4", "-bs", "5", "-ojf", "-of", str(output), "-np", "-nt"], temp, 180,
                    cancel=cancel, lock_fd=lock.fileno())
                result_path = output.with_suffix(".json")
                if result_path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("output_limit")
                text, language = parse_result(json.loads(result_path.read_text()))
                return {"status": "transcribed", "text": text, "language": language,
                        "engine": "whisper.cpp", "model": Path(config["model"]).name,
                        "audio_sha256": hashlib.sha256(data).hexdigest(), "duration_seconds": round(duration, 2),
                        "elapsed_seconds": round(time.monotonic() - started, 2), "untrusted": True}
    except subprocess.TimeoutExpired:
        return unavailable("local_decoder_timeout")
    except (OSError, ValueError, KeyError, TypeError, wave.Error, OverflowError):
        # Do not leak file contents, process output or configuration through errors.
        return unavailable("local_decode_failed_or_unclear")


if __name__ == "__main__":
    if len(sys.argv) < 5 or sys.argv[1] != "--supervise":
        raise SystemExit("Internal decoder supervisor only")
    raise SystemExit(supervise(sys.argv[4:], int(sys.argv[2]), float(sys.argv[3])))
