#!/usr/bin/env python3
"""Private Telegram inbox. No model calls, shell commands or Codex Queue calls."""
import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from history_safety import redact
from telegram_format import delivery_plan, receipt_entities_match
from operator_policy import ROOT, BOT, OWNER_LABEL, CONTACT_LABEL, INVITE_FILES, CHIEF_NAME, CHIEF_NICKNAME

PREFIX = ""  # Telegram has its own bot identity; the Alfred prefix is WhatsApp-only.
MAX_TEXT = 4000  # Telegram part size only; never a task or complete-answer limit.
MAX_ATTACHMENT = 20 * 1024 * 1024  # Telegram Bot API getFile download limit.
MAX_ATTACHMENT_STORAGE = 100 * 1024 * 1024  # Per enrolled contact; the owner is bounded by free disk only.
ATTACHMENT_FREE_RESERVE = 2 * 1024 ** 3  # Never fill the host disk with received files.
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4", ".wav", ".webm", ".flac", ".aac"}
AUDIO_MIME_EXTENSIONS = {"audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3",
                         "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
                         "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/flac": ".flac",
                         "audio/aac": ".aac"}
FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".txt", ".csv",
                   ".md", ".json", ".docx", ".xlsx", ".pptx", ".zip"}
VIDEO_KINDS = ("video", "video_note", "animation")
COMMANDS = {"/start", "/help", "/setup", "/status", "/report", "/done"}
DELIVERY_NOTICE = "Your messages are saved while Chief's direct connection is unavailable."
INVITE_CODE = re.compile(r"(?<![A-Za-z0-9_-])invite_[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])")
INVITE_LIKE = re.compile(r"(?<![A-Za-z0-9_-])invite_[A-Za-z0-9_-]+")


class BridgeError(Exception):
    def __init__(self, message, status=None, retry_after=None, not_sent=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.not_sent = not_sent


class TelegramConnectError(OSError):
    """Connection failed before an HTTP request could be transmitted."""


class ReplyQueueFull(BridgeError):
    """Ingestion was refused before the Telegram update was acknowledged."""


def text_parts(text):
    """Keep every character, prefer line boundaries, and count UTF-16 units."""
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise BridgeError("A nonempty text reply without NUL characters is required")
    try:
        units = len(text.encode("utf-16-le")) // 2
    except UnicodeError:
        raise BridgeError("Reply contains invalid Unicode") from None
    if units <= MAX_TEXT:
        return [text]
    chunks, start, size, last_break = [], 0, 0, 0
    # Reserve space for a part number. Do not split surrogate pairs.
    for index, char in enumerate(text):
        cost = 2 if ord(char) > 0xFFFF else 1
        if size + cost > MAX_TEXT - 32:
            end = last_break if last_break > start else index
            chunks.append(text[start:end])
            start, last_break = end, 0
            size = len(text[start:index].encode("utf-16-le")) // 2
        size += cost
        if char == "\n":
            last_break = index + 1
    chunks.append(text[start:])
    return [f"[{i}/{len(chunks)}]\n{chunk}" for i, chunk in enumerate(chunks, 1)]


def photo_reply(key, body):
    """Parse a result photo without fetching its URL or opening a local file.

    Use ![alt](https://public-host/image.png) on the first line, followed by
    a plain caption. Long captions are sent as following text messages. URL credentials, query strings,
    fragments and local hosts are unsupported. Other replies stay text.
    """
    first, _, caption = body.partition("\n")
    if not key.startswith("result:") or not first.lstrip().startswith("!["):
        return None
    match = re.fullmatch(r"!\[[^\[\]\r\n]*\]\(([^\s()<>]+)\)", first)
    if not match:
        raise BridgeError("Use one first-line image: ![alt](https://public-host/image.png)")
    url = match[1]
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.port not in (None, 443) or "?" in url or "#" in url or "\\" in url
                or not parts.path or re.search(r"[\x00-\x20\x7f]", urllib.parse.unquote(url))):
            raise ValueError
        try:
            public = ipaddress.ip_address(host).is_global
        except ValueError:
            public = bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host))
            public = public and not host.endswith((".localhost", ".local", ".internal", ".home", ".arpa", ".test", ".invalid", ".example", ".onion"))
        if not public:
            raise ValueError
        _, counts = redact(urllib.parse.unquote(body), conservative=False)
        if any(name not in {"email", "long_token"} for name in counts):
            raise ValueError
        caption.encode("utf-16-le")
        if re.search(r"(?m)^\s*!\[", caption):
            raise ValueError
    except (ValueError, UnicodeError):
        raise BridgeError("Photo replies need one public HTTPS URL without credentials, query or fragment, "
                          "and a plain text caption.") from None
    return {"photo": url, "caption": caption}


def report_first_part(key, text):
    """Keep BACKLOG first on the owner's report, including multipart replies."""
    if key.startswith("result:report:"):
        return re.sub(r"^\[1/[0-9]+\]\n(?=BACKLOG(?:\s|\())", "", text, count=1)
    return text


def reply_parts(key, body):
    parts = text_parts(body)  # Validate the complete body before storing it.
    photo = photo_reply(key, body)
    if photo:
        if len(photo["caption"].encode("utf-16-le")) // 2 <= 1024:
            return [body]
        # Send the image once, then its complete caption as ordinary text.
        return [body.partition("\n")[0], *text_parts(photo["caption"])]
    parts[0] = report_first_part(key, parts[0])
    return parts


def receipt_text_matches(actual, expected):
    # TDLib removes boundary ASCII spaces/newlines from plain text. Keep the
    # original answer intact; accept only that normalization, never changed
    # internal whitespace, missing content or other Unicode normalization.
    return isinstance(actual, str) and actual in (expected, expected.strip(" \n"))


def wire_body(item):
    """A stable per-part body for the existing durable receipt mechanism."""
    if item["method"] == "sendMessage":
        return item["payload"]["text"]
    if item["method"] == "sendPhoto":
        return json.dumps(item["payload"], sort_keys=True)
    raise BridgeError("Unsupported reply payload")


def valid_text_receipt(result, chat, text, entities=None):
    return (isinstance(result, dict) and receipt_text_matches(result.get("text"), text)
            and receipt_entities_match(result.get("entities"), entities, text, result.get("text"))
            and isinstance(result.get("chat"), dict)
            and type(result["chat"].get("id")) is int and result["chat"]["id"] == chat
            and type(result.get("message_id")) is int and result["message_id"] > 0)


def valid_photo_receipt(result, chat, caption, entities=None):
    if (not isinstance(result, dict) or not receipt_text_matches(result.get("caption", ""), caption)
            or not receipt_entities_match(result.get("caption_entities"), entities, caption, result.get("caption", ""))
            or not isinstance(result.get("chat"), dict) or type(result["chat"].get("id")) is not int
            or result["chat"]["id"] != chat or type(result.get("message_id")) is not int
            or result["message_id"] <= 0):
        return False
    sizes = result.get("photo")
    return isinstance(sizes, list) and bool(sizes) and all(
        isinstance(size, dict)
        and all(isinstance(size.get(key), str) and bool(size[key].strip()) for key in ("file_id", "file_unique_id"))
        and all(type(size.get(key)) is int and size[key] > 0 for key in ("width", "height"))
        and ("file_size" not in size or (type(size["file_size"]) is int and size["file_size"] > 0))
        for size in sizes)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def connect_telegram(address, timeout=40, source_address=None):
    """Prefer working IPv4 here without changing the Mac's network settings."""
    try:
        addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    except OSError:
        raise TelegramConnectError("Telegram name resolution unavailable") from None
    addresses.sort(key=lambda info: info[0] != socket.AF_INET)
    deadline = time.monotonic() + 10
    for family, kind, proto, _, endpoint in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(family, kind, proto)
        try:
            connection.settimeout(min(5, remaining, timeout))
            if source_address:
                connection.bind(source_address)
            connection.connect(endpoint)
            connection.settimeout(timeout)
            return connection
        except OSError:
            connection.close()
    raise TelegramConnectError("Telegram connection unavailable")


class TelegramHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self._create_connection = connect_telegram
        super().connect()


class TelegramHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(TelegramHTTPSConnection, request, context=self._context)


class Telegram:
    def __init__(self, root, *, token=None):
        if token is None:
            path = root / "bot-token"
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BridgeError("Credential file must be an owner-only regular file")
            token = path.read_text().strip()
        self.token = token
        if not self.token:
            raise BridgeError("Missing bot credential")
        self.opener = urllib.request.build_opener(NoRedirect, TelegramHTTPSHandler)

    def call(self, method, payload=None):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "sendMessage", "sendPhoto", "getFile",
                          "getMyName", "setMyName", "getMyCommands", "setMyCommands",
                          "getChatMenuButton", "setChatMenuButton"}:
            raise BridgeError("Unsupported Telegram operation")
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self.token + "/" + method,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=40) as response:
                result = json.loads(response.read(1_000_000))
        except urllib.error.HTTPError as error:
            retry_after = None
            if error.code == 429:
                try:
                    retry_after = int(json.loads(error.read(10000)).get("parameters", {}).get("retry_after", 30))
                except (ValueError, TypeError, OSError):
                    retry_after = 30
            raise BridgeError("Telegram HTTP " + str(error.code), error.code, retry_after) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TelegramConnectError):
                raise BridgeError("Telegram connection unavailable", not_sent=True) from None
            raise BridgeError("Telegram request failed; delivery may be uncertain") from None
        except (OSError, ValueError):
            raise BridgeError("Telegram request failed; delivery may be uncertain") from None
        if not result.get("ok"):
            raise BridgeError("Telegram rejected request")
        return result["result"]

    def download(self, file_id, max_bytes):
        """Return bounded opaque bytes. Never expose the credential-bearing URL."""
        try:
            info = self.call("getFile", {"file_id": file_id})
            path = info.get("file_path", "")
            size = info.get("file_size")
            if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,1024}", path)
                    or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise BridgeError("Attachment download unavailable")
            if isinstance(size, int) and size > max_bytes:
                raise BridgeError("Attachment exceeds the download limit")
            request = urllib.request.Request("https://api.telegram.org/file/bot" + self.token + "/" + path)
            with self.opener.open(request, timeout=40) as response:
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > max_bytes:
                    raise BridgeError("Attachment exceeds the download limit")
                data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise BridgeError("Attachment exceeds the download limit")
            if ((length is not None and len(data) != int(length))
                    or (isinstance(size, int) and len(data) != size)):
                raise BridgeError("Attachment download incomplete")
            return data
        except (BridgeError, OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            # Neither Telegram errors nor urllib errors are safe to persist here.
            raise BridgeError("Attachment download failed. Please resend a file smaller than 10 MiB") from None


class Inbox:
    def __init__(self, root=ROOT):
        os.umask(0o077)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "inbox.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invites (
              digest TEXT PRIMARY KEY, label TEXT NOT NULL, expires REAL NOT NULL, used_by INTEGER);
            CREATE TABLE IF NOT EXISTS users (
              user_id INTEGER PRIMARY KEY, chat_id INTEGER UNIQUE NOT NULL,
              label TEXT NOT NULL, added REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
              id TEXT PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL, user_id INTEGER NOT NULL,
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, label TEXT NOT NULL,
              body TEXT NOT NULL, received REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', telegram_id INTEGER, created REAL NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS outbox_parts (
              outbox_id TEXT NOT NULL, part_index INTEGER NOT NULL, body TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', telegram_id INTEGER,
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(outbox_id, part_index));
            CREATE TABLE IF NOT EXISTS attachments (
              request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, local_path TEXT,
              size INTEGER, status TEXT NOT NULL, error TEXT);
            CREATE INDEX IF NOT EXISTS request_user_time ON requests(user_id,received);
            CREATE INDEX IF NOT EXISTS reply_chat_time ON outbox(chat_id,created);
        """)
        with self.db:
            request_columns = {row[1] for row in self.db.execute("PRAGMA table_info(requests)")}
            if "reply_to_id" not in request_columns:
                self.db.execute("ALTER TABLE requests ADD COLUMN reply_to_id INTEGER")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
            for column, spec in (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt", "REAL NOT NULL DEFAULT 0"),
                                 ("delivery_plan", "TEXT")):
                if column not in columns:
                    self.db.execute("ALTER TABLE outbox ADD COLUMN " + column + " " + spec)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('offset','0')")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('started',?)", (str(int(time.time())),))

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def delivery_notice(self):
        if self.meta("appserver_enabled") == "1":
            if self.meta("appserver_health") == "connected":
                return "Your messages reach Chief directly."
            return "Your messages are saved while Chief's direct connection recovers."
        return DELIVERY_NOTICE

    def invitation(self, label=CONTACT_LABEL):
        if label not in INVITE_FILES:
            raise BridgeError("Only the configured contacts can be enrolled")
        if self.db.execute("SELECT 1 FROM users WHERE label=?", (label,)).fetchone():
            raise BridgeError("This contact is already enrolled; verify identity changes before replacing access")
        token = "invite_" + secrets.token_urlsafe(24)
        with self.db:
            self.db.execute("DELETE FROM invites WHERE used_by IS NULL AND label=?", (label,))
            self.db.execute("INSERT INTO invites VALUES (?,?,?,NULL)",
                            (hashlib.sha256(token.encode()).hexdigest(), label, time.time() + 7 * 86400))
        path = self.root / INVITE_FILES[label]
        path.write_text(PREFIX + label + ", connect your private inbox here: https://t.me/" + BOT + "?start=" + token +
                        "\nTap Start, then type your request. " + self.delivery_notice() + "\n")
        path.chmod(0o600)
        return str(path)

    def outstanding_parts(self, chat):
        return self.db.execute("""SELECT COALESCE(SUM(CASE
            WHEN EXISTS(SELECT 1 FROM outbox_parts p WHERE p.outbox_id=o.id)
            THEN (SELECT COUNT(*) FROM outbox_parts p WHERE p.outbox_id=o.id AND p.status IN ('pending','sending'))
            ELSE 1 END),0) FROM outbox o WHERE chat_id=? AND status IN ('pending','sending')""", (chat,)).fetchone()[0]

    def owner_chat(self, chat):
        """The enrolled installation owner's own private chat, never a display name."""
        return self.db.execute("SELECT 1 FROM users WHERE chat_id=? AND user_id=chat_id AND label=?",
                               (chat, OWNER_LABEL)).fetchone() is not None

    def suppress(self, key, chat, text):
        """Save a complete answer the coordinator chose not to send. Flush never claims it."""
        return self.db.execute("INSERT OR IGNORE INTO outbox(id,chat_id,body,status,created) VALUES (?,?,?,'suppressed',?)",
                               (key, chat, PREFIX + text, time.time())).rowcount

    def reply(self, key, chat, text):
        parts = reply_parts(key, PREFIX + text)
        if self.db.execute("SELECT 1 FROM outbox WHERE id=?", (key,)).fetchone():
            return
        plan = delivery_plan(PREFIX + text, text_parts, photo_reply(key, PREFIX + text))
        if plan:
            if plan[0]["method"] == "sendMessage":
                payload = plan[0]["payload"]
                first = report_first_part(key, payload["text"])
                removed = len(payload["text"]) - len(first)  # The removed counter is ASCII.
                if removed:
                    payload["text"] = first
                    payload["entities"] = [entity | {"offset": entity["offset"] - removed}
                                           for entity in payload.get("entities", [])]
            parts = [wire_body(item) for item in plan]
        # The installation owner's own replies are never dropped or refused by a
        # count: each is durable, ordered and paced by Telegram's own 429 backoff.
        if not self.owner_chat(chat):
            outstanding = self.outstanding_parts(chat)
            # Backpressure counts replies still queued to send, not the length of one
            # answer, nor failed/uncertain records that wait for reconciliation.
            pending_replies = self.db.execute("SELECT COUNT(*) FROM outbox WHERE chat_id=? AND status IN ('pending','sending')", (chat,)).fetchone()[0]
            if pending_replies >= 250:
                if key.startswith(("result:", "welcome:", "ack:")):
                    raise ReplyQueueFull("Reply queue is full; keep the request uncompleted until delivery is repaired")
                return
            if not key.startswith(("result:", "welcome:", "ack:")):
                recent = self.db.execute("SELECT COUNT(*) FROM outbox WHERE chat_id=? AND created>?", (chat, time.time() - 3600)).fetchone()[0]
                if recent >= 60 or outstanding >= 200:
                    return  # Bound command/rejection responses as well as accepted feedback.
        inserted = self.db.execute("INSERT OR IGNORE INTO outbox(id,chat_id,body,created,delivery_plan) VALUES (?,?,?,?,?)",
                                   (key, chat, PREFIX + text, time.time(), json.dumps(plan) if plan else None)).rowcount
        if not inserted:
            return  # A concurrent enqueue already owns this answer and its receipts.
        # Keep the original body and aggregate receipt for existing callers.
        self.db.execute("DELETE FROM outbox_parts WHERE outbox_id=?", (key,))
        if len(parts) > 1:
            self.db.executemany("INSERT INTO outbox_parts(outbox_id,part_index,body) VALUES (?,?,?)",
                                [(key, index, body) for index, body in enumerate(parts)])

    def _activate_invitation(self, uid, cid, token):
        """Validate a private invitation inside the caller's database transaction."""
        if (not isinstance(uid, int) or uid <= 0 or uid != cid
                or not INVITE_CODE.fullmatch(token)
                or self.db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()):
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        invite = self.db.execute("SELECT * FROM invites WHERE digest=? AND used_by IS NULL AND expires>?",
                                 (digest, time.time())).fetchone()
        if not invite or invite["label"] not in INVITE_FILES:
            return False
        changed = self.db.execute(
            "UPDATE invites SET used_by=? WHERE digest=? AND used_by IS NULL AND expires>? "
            "AND NOT EXISTS (SELECT 1 FROM users WHERE label=?)",
            (uid, digest, time.time(), invite["label"])).rowcount
        if not changed:
            return False
        self.db.execute("INSERT INTO users VALUES (?,?,?,?)", (uid, cid, invite["label"], time.time()))
        self.reply("welcome:" + str(uid), cid,
                   "Your private inbox is connected. " + self.delivery_notice() +
                   " Please resend any earlier feedback. Your messages are labelled as yours. "
                   "Do not send passwords or keys.")
        return True

    def _attachment(self, key, message, api, owner=False, uid=None):
        """Keep untrusted files private and opaque. Never execute or unpack them."""
        document = message.get("document")
        video = next((kind for kind in VIDEO_KINDS if isinstance(message.get(kind), dict)), None)
        audio_kind = next((kind for kind in ("voice", "audio") if isinstance(message.get(kind), dict)), None)
        if not audio_kind and isinstance(document, dict):
            mime = str(document.get("mime_type", "")).split(";", 1)[0].strip().lower()
            if mime.startswith("audio/") or Path(str(document.get("file_name", ""))).suffix.lower() in AUDIO_EXTENSIONS:
                audio_kind = "document"
        if audio_kind:
            source, kind = message[audio_kind], "audio" if audio_kind == "document" else audio_kind
            suffix = Path(str(source.get("file_name", ""))).suffix.lower()
            mime = str(source.get("mime_type", "")).split(";", 1)[0].strip().lower()
            suffix = suffix if suffix in AUDIO_EXTENSIONS else AUDIO_MIME_EXTENSIONS.get(mime)
            suffix = suffix or (".ogg" if kind == "voice" else ".bin")
        elif isinstance(document, dict):
            source, kind = document, "document"
            suffix = Path(str(source.get("file_name", ""))).suffix.lower()
            suffix = suffix if suffix in FILE_EXTENSIONS else ".bin"
        elif video:
            # Saved as an opaque private file like a document; its caption is the request.
            source, kind, suffix = message[video], "video", ".mp4"
        else:
            photos = [p for p in message.get("photo", []) if isinstance(p, dict)]
            source = max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0), default={})
            kind, suffix = "photo", ".jpg"
        destination = None
        failure = "Attachment not received. Please resend it (Telegram lets bots download files up to 20 MiB), or send text"
        try:
            if audio_kind:
                duration = source.get("duration")
                # Telegram documents usually have no duration metadata. Any length
                # is saved; the byte limit and the decoder's own capacity apply.
                if (audio_kind != "document" or duration is not None) and (
                        isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration < float("inf")):
                    failure = "Audio duration is invalid. Please resend the recording or send text"
                    raise BridgeError(failure)
                if suffix == ".bin":
                    failure = "Audio format unavailable. Please send a voice note, MP3, M4A, OGG or WAV recording"
                    raise BridgeError(failure)
            if not isinstance(source.get("file_id"), str) or not source["file_id"] or api is None:
                raise BridgeError("Attachment download unavailable. Please resend the file")
            declared = source.get("file_size")
            if isinstance(declared, int) and declared > MAX_ATTACHMENT:
                failure = "Attachment exceeds Telegram's 20 MiB bot download limit. Please send a smaller file or a link"
                raise BridgeError(failure)
            directory = self.root / "attachments"
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                failure = "Private attachment storage unavailable. Please send text for now"
                raise BridgeError(failure)
            available = min(MAX_ATTACHMENT, shutil.disk_usage(directory).free - ATTACHMENT_FREE_RESERVE)
            if not owner:
                used = self.db.execute("SELECT COALESCE(SUM(a.size),0) FROM attachments a JOIN requests r ON r.id=a.request_id "
                                       "WHERE r.user_id=? AND a.status='ready'", (uid,)).fetchone()[0]
                available = min(available, MAX_ATTACHMENT_STORAGE - used)
            if available <= 0 or (isinstance(declared, int) and declared > available):
                failure = "Private attachment storage is full. Please send text for now"
                raise BridgeError(failure)
            data = api.download(source["file_id"], available)
            if not isinstance(data, bytes) or len(data) > available or (audio_kind and not data):
                raise BridgeError("Attachment exceeds the storage limit. Please send a smaller file")
            destination = directory / (hashlib.sha256(key.encode()).hexdigest() + suffix)
            # The name comes from our request ID, never from sender-controlled paths.
            with destination.open("wb") as output:
                output.write(data)
            destination.chmod(0o600)
            result = (key, kind, str(destination.resolve()), len(data), "ready", None)
        except (BridgeError, OSError):
            if destination is not None:
                destination.unlink(missing_ok=True)
            # Fixed messages prevent private URLs, file IDs or tokens entering records.
            result = (key, kind, None, None, "failed", failure)
        self.db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?)", result)
        return result[4] == "ready"

    def ingest(self, update, api=None):
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        with self.db:
            if update_id < int(self.meta("offset", "0")):
                return
            self.set_meta("offset", update_id + 1)
            # An edit is new owner input, saved under its own ID; the original stays.
            edited = not update.get("message") and isinstance(update.get("edited_message"), dict)
            message = update.get("message") or update.get("edited_message") or {}
            sender, chat = message.get("from") or {}, message.get("chat") or {}
            uid, cid = sender.get("id"), chat.get("id")
            if (chat.get("type") != "private" or not isinstance(uid, int) or uid != cid
                    or sender.get("is_bot") or message.get("date", 0) < float(self.meta("started"))):
                return
            has_attachment = (any(isinstance(message.get(kind), dict) for kind in ("document", "voice", "audio", *VIDEO_KINDS))
                              or bool(message.get("photo")))
            text = message.get("text", message.get("caption", "") if has_attachment else "")
            if not isinstance(text, str):
                return
            user = self.db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            # Accept the exact private code in /start, a pasted link, or ordinary text.
            # Never retain an activation attempt as project feedback.
            codes = set(INVITE_CODE.findall(text))
            if not user and len(codes) == 1 and self._activate_invitation(uid, cid, codes.pop()):
                if api is not None:
                    try:
                        configure_chat_menu(api, cid, self.db.execute("SELECT label FROM users WHERE user_id=?", (uid,)).fetchone()[0])
                    except BridgeError:
                        self.set_meta("presentation_health", "menu_setup_failed")
                return
            if not user or user["label"] not in INVITE_FILES:
                # Unknown senders get no response, stored request or model access.
                return
            key = "tg:" + str(cid) + ":" + str(message.get("message_id"))
            original = key
            if edited:
                if text.strip().split(" ", 1)[0] in COMMANDS:
                    return  # Editing a command does not run it again.
                key += ":edit:" + str(message.get("edit_date") if isinstance(message.get("edit_date"), int) else update_id)
            if self.db.execute("SELECT 1 FROM requests WHERE id=?", (key,)).fetchone():
                return
            if INVITE_LIKE.search(text):
                self.reply(key, cid, "Your private inbox is already connected. Send text, a photo, a document or a voice note.")
                return
            if text.strip() in {"/start", "/help"}:
                self.reply(key, cid, "Send text, photos, videos, documents or voice/audio up to 20 MiB each (Telegram's bot download limit). " + self.delivery_notice() + " Use /setup for connection and installation guides, or /status to check the inbox.")
                return
            if text.strip() == "/setup":
                self.reply("result:setup:" + key, cid, setup_guides())
                return
            if text.strip() == "/status":
                pending = self.db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND status NOT IN ('done','denied')", (uid,)).fetchone()[0]
                self.reply(key, cid, "Inbox online. " + str(pending) + " request(s) awaiting completion. " + self.delivery_notice())
                return
            if text.strip() == "/report" and user["label"] == OWNER_LABEL:
                from alfred_report import report
                try:
                    body = report(db=self.db)
                except Exception:
                    logging.exception("Report generation failed")
                    body = "Report unavailable. The saved task board is still intact."
                # One parent preserves order and completion across all report parts.
                self.reply("result:report:" + key, cid, body)
                return
            done_page = re.fullmatch(r"/done(?:\s+(\S+))?", text.strip())
            if done_page and user["label"] == OWNER_LABEL:
                from alfred_report import done
                page = int(done_page[1]) if done_page[1] and re.fullmatch(r"[1-9][0-9]{0,5}", done_page[1]) else (
                    1 if done_page[1] is None else None)
                try:
                    body = done(page=page)
                except Exception:
                    logging.exception("Done list generation failed")
                    body = "Done list unavailable. The saved task board is still intact."
                self.reply("result:done:" + key, cid, body)
                return
            if not text and not has_attachment:
                self.reply(key, cid, "This message type isn't supported. Please send text, a photo, a video, a document or voice/audio.")
                return
            sanitized, counts = redact(text, conservative=False)
            removed = any(name not in {"email", "long_token"} for name in counts)
            # Never store the credential-like part; keep the rest instead of losing the message.
            if removed and not has_attachment and not re.search(r"\w", sanitized.replace("[REDACTED]", "")):
                self.reply(key, cid, "This looks like it contains a key or password. Please resend the feedback without credentials.")
                return
            if edited:
                sanitized = "[Edited Telegram message; earlier version: " + original + "]\n" + sanitized
            recent = self.db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND received>?", (uid, time.time() - 3600)).fetchone()[0]
            waiting = self.db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND status NOT IN ('done','denied')", (uid,)).fetchone()[0]
            if user["label"] != OWNER_LABEL and (recent >= 30 or waiting >= 50):
                self.reply(key, cid, "Your earlier feedback is saved. Please wait for it to be handled before sending more.")
                return
            pending_replies = self.db.execute("SELECT COUNT(*) FROM outbox WHERE chat_id=? AND status IN ('pending','sending')", (cid,)).fetchone()[0]
            if pending_replies >= 250 and user["label"] != OWNER_LABEL:
                raise ReplyQueueFull("Reply queue is full; keep the request uncompleted until delivery is repaired")
            parent = message.get("reply_to_message") or {}
            parent_id = parent.get("message_id") if isinstance(parent, dict) else None
            if not isinstance(parent_id, int) or parent_id <= 0:
                parent_id = None
            self.db.execute("INSERT OR IGNORE INTO requests(id,update_id,user_id,chat_id,message_id,label,body,received,reply_to_id) VALUES (?,?,?,?,?,?,?,?,?)",
                            (key, update_id, uid, cid, message.get("message_id"), user["label"], sanitized, time.time(), parent_id))
            notices = []
            if removed:
                notices.append("Your message is saved without a part that looked like a password, key or code; "
                               "that part was not stored. Reword it without the secret if Chief needs it.")
            if has_attachment and not self._attachment(key, message, api, user["label"] == OWNER_LABEL, uid):
                failure = self.db.execute("SELECT error FROM attachments WHERE request_id=?", (key,)).fetchone()[0]
                notices.append("Your message is saved, but the attachment was not received. " + failure)
            if notices:
                try:
                    self.reply("ack:" + key, cid, "\n".join(notices))
                except ReplyQueueFull:
                    if user["label"] != OWNER_LABEL:
                        raise

    def flush(self, api):
        # Parent status is aggregate: every part needs its own exact receipt.
        # A failed/uncertain part holds later parts. Never replay an uncertain send.
        delivered = False
        budget = 20
        for row in self.db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? ORDER BY created LIMIT 20", (time.time(),)).fetchall():
            try:
                # Freeze rendered text/entities at enqueue time. Existing queued
                # replies retain their original plan, including after an upgrade.
                plan = json.loads(row["delivery_plan"]) if row["delivery_plan"] else None
                bodies = [wire_body(item) for item in plan] if plan else reply_parts(row["id"], row["body"])
            except (BridgeError, ValueError, TypeError, KeyError):
                with self.db:
                    self.db.execute("UPDATE outbox SET status='failed' WHERE id=? AND status='pending'", (row["id"],))
                continue
            parts = self.db.execute("SELECT * FROM outbox_parts WHERE outbox_id=? ORDER BY part_index", (row["id"],)).fetchall()
            if not parts and len(bodies) > 1:
                # Old queued rows also receive a durable plan before the first send.
                with self.db:
                    self.db.executemany("INSERT OR IGNORE INTO outbox_parts(outbox_id,part_index,body) VALUES (?,?,?)",
                                        [(row["id"], i, body) for i, body in enumerate(bodies)])
                parts = self.db.execute("SELECT * FROM outbox_parts WHERE outbox_id=? ORDER BY part_index", (row["id"],)).fetchall()
            if parts and ([part["body"] for part in parts] != bodies
                          or [part["part_index"] for part in parts] != list(range(len(bodies)))):
                with self.db:
                    self.db.execute("UPDATE outbox SET status='failed' WHERE id=?", (row["id"],))
                continue
            for index, part in enumerate(parts or [row]):
                if part["status"] == "sent":
                    continue
                if part["status"] != "pending" or part["next_attempt"] > time.time() or budget <= 0:
                    break
                # Enrollment is checked again before every network send, including
                # notices queued by an older collector. Display names grant no access.
                allowed = self.db.execute("""SELECT label FROM users WHERE user_id=chat_id
                    AND chat_id=? AND added<=? AND label IN (?,?)""",
                    (row["chat_id"], row["created"], OWNER_LABEL, CONTACT_LABEL)).fetchone()
                if (not allowed or row["id"].startswith("access-help:")
                        or (row["id"].startswith("result:maintenance:") and allowed["label"] != OWNER_LABEL)):
                    with self.db:
                        self.db.execute("UPDATE outbox SET status='failed' WHERE id=? AND status='pending'", (row["id"],))
                    break
                wire = plan[index] if plan else None
                photo = (wire["payload"] if wire and wire["method"] == "sendPhoto" else
                         None if wire else photo_reply(row["id"], part["body"]))
                payload = (wire["payload"] if wire else photo if photo else
                           {"text": part["body"], "link_preview_options": {"is_disabled": True}})
                with self.db:
                    # Collector and adapter may both flush. Claim the current part
                    # inside the write transaction, not from its cached status.
                    condition = ""
                    claim = (row["id"], time.time())
                    if parts:
                        condition = """ AND EXISTS(SELECT 1 FROM outbox_parts p WHERE p.outbox_id=outbox.id
                            AND p.part_index=? AND p.status='pending' AND p.next_attempt<=?)
                            AND NOT EXISTS(SELECT 1 FROM outbox_parts p WHERE p.outbox_id=outbox.id
                            AND p.part_index<? AND p.status!='sent')"""
                        claim += (part["part_index"], time.time(), part["part_index"])
                    changed = self.db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 "
                        "WHERE id=? AND status='pending' AND next_attempt<=?" + condition, claim).rowcount
                    if changed and parts:
                        self.db.execute("UPDATE outbox_parts SET status='sending',attempts=attempts+1 WHERE outbox_id=? AND part_index=?",
                                        (row["id"], part["part_index"]))
                if not changed:
                    break
                budget -= 1
                state, next_attempt, telegram_id = "uncertain", 0, None
                try:
                    result = api.call("sendPhoto" if photo else "sendMessage", {"chat_id": row["chat_id"], **payload})
                    verified = (valid_photo_receipt(result, row["chat_id"], payload["caption"], payload.get("caption_entities")) if photo else
                                valid_text_receipt(result, row["chat_id"], payload["text"], payload.get("entities")))
                    if verified:
                        state, telegram_id = "sent", result["message_id"]
                except BridgeError as error:
                    if error.status == 429 or error.not_sent:
                        delay = max(1, min(error.retry_after or 30 * 2 ** min(part["attempts"], 7), 3600))
                        state, next_attempt = "pending", time.time() + delay
                    elif error.status is not None and 400 <= error.status < 500:
                        state = "failed"
                with self.db:
                    parent_state = state
                    if parts:
                        self.db.execute("UPDATE outbox_parts SET status=?,next_attempt=?,telegram_id=? WHERE outbox_id=? AND part_index=?",
                                        (state, next_attempt, telegram_id, row["id"], part["part_index"]))
                        if state == "sent" and part["part_index"] < len(parts) - 1:
                            parent_state, telegram_id = "pending", None
                    self.db.execute("UPDATE outbox SET status=?,next_attempt=?,telegram_id=? WHERE id=?",
                                    (parent_state, next_attempt, telegram_id, row["id"]))
                delivered = delivered or parent_state == "sent"
                if state != "sent":
                    break
            if budget <= 0:
                break
        if delivered:
            from alfred_outcomes import reconcile_delivered_duplicates
            reconcile_delivered_duplicates(self.db)

    def read(self):
        rows = self.db.execute("SELECT id,label,body,received,status FROM requests WHERE status='pending' ORDER BY received LIMIT 10").fetchall()
        processing = self.db.execute("SELECT id,label,received FROM requests WHERE status='processing' ORDER BY received LIMIT 10").fetchall()
        uncertain = self.db.execute("SELECT COUNT(*) FROM outbox WHERE status IN ('sending','uncertain')").fetchone()[0]
        failed = self.db.execute("SELECT COUNT(*) FROM outbox WHERE status='failed'").fetchone()[0]
        pending_replies = self.db.execute("SELECT COUNT(*),MIN(created) FROM outbox WHERE status='pending'").fetchone()
        pending_results = self.db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending' AND id LIKE 'result:%'").fetchone()[0]
        open_requests = self.db.execute("SELECT COUNT(*),MIN(received) FROM requests WHERE status NOT IN ('done','denied')").fetchone()
        requests = [dict(row) for row in rows]
        for request in requests:
            request["attachments"] = [dict(row) | {"untrusted": True} for row in self.db.execute(
                "SELECT kind,local_path,size,status,error FROM attachments WHERE request_id=?", (request["id"],))]
        return {"requests": requests, "processing": [dict(row) for row in processing],
                "uncertain_replies": uncertain, "failed_replies": failed,
                "pending_replies": pending_replies[0], "oldest_pending_reply": pending_replies[1],
                "pending_result_replies": pending_results,
                "open_request_count": open_requests[0], "oldest_open_request": open_requests[1],
                "last_poll": self.meta("last_poll"), "health": self.meta("health", "not_started"),
                "chat_delivery": "appserver" if self.meta("appserver_enabled") == "1" else "unavailable",
                "appserver_health": self.meta("appserver_health", "disabled"),
                "appserver_checked": self.meta("appserver_checked"),
                "appserver_outbox_health": self.meta("appserver_outbox_health")}

    def reopen(self, identifier):
        with self.db:
            return self.db.execute("UPDATE requests SET status='pending' WHERE id=? AND status='processing'", (identifier,)).rowcount == 1

    def claim(self, identifier):
        with self.db:
            return self.db.execute("UPDATE requests SET status='processing' WHERE id=? AND status='pending'", (identifier,)).rowcount == 1

    def finish(self, identifier, reply):
        reply_parts("result:" + identifier, PREFIX + reply)
        with self.db:
            row = self.db.execute("SELECT * FROM requests WHERE id=?", (identifier,)).fetchone()
            if not row or row["status"] != "processing":
                raise BridgeError("Request is not claimed or was already completed")
            self.reply("result:" + identifier, row["chat_id"], reply)
            self.db.execute("UPDATE requests SET status='done' WHERE id=?", (identifier,))

    def expire_completed(self, cutoff):
        """Apply the existing completed-request retention to their private files."""
        directory = (self.root / "attachments").resolve()
        rows = self.db.execute("SELECT a.local_path FROM attachments a JOIN requests r ON r.id=a.request_id "
                               "WHERE r.status IN ('done','denied') AND r.received<?", (cutoff,)).fetchall()
        for row in rows:
            if row["local_path"]:
                path = Path(row["local_path"])
                if path.parent != directory or not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]+", path.name):
                    raise BridgeError("Attachment retention path invalid")
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    raise BridgeError("Attachment retention unavailable") from None
        self.db.execute("DELETE FROM attachments WHERE request_id IN "
                        "(SELECT id FROM requests WHERE status IN ('done','denied') AND received<?)", (cutoff,))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_audio'").fetchone():
            self.db.execute("DELETE FROM appserver_audio WHERE request_id IN "
                            "(SELECT id FROM requests WHERE status IN ('done','denied') AND received<?)", (cutoff,))
        self.db.execute("DELETE FROM requests WHERE status IN ('done','denied') AND received<?", (cutoff,))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_entry_requests'").fetchone():
            self.db.execute("DELETE FROM device_entry_requests WHERE request_id NOT IN (SELECT id FROM requests)")


def setup_guides():
    return ("Chief setup\n\nhttps://usetempl.com/chief\n\n"
            "1. Install Chief on your main computer.\n"
            "2. Connect Dot for phone access.\n"
            "3. Add devices and assign work.\n"
            "4. Use /report for the saved board, without AI tokens.\n\n"
            "Give this page to your AI and choose a path.\n"
            "Public instructions grant no access. Another person uses their own account and private instance.")


def configure_chat_menu(api, chat_id, label):
    if label not in INVITE_FILES:
        raise BridgeError("Only enrolled contacts receive a command menu")
    commands = ([{"command": "report", "description": "Open tasks grouped by project lead"},
                 {"command": "done", "description": "Completed tasks, 20 per page"}]
                if label == OWNER_LABEL else [])
    commands += [{"command": "setup", "description": "Connect a device or start a separate Chief"}]
    scope = {"type": "chat", "chat_id": chat_id}
    for language in ("", "en", "pt"):
        query = {"scope": scope, "language_code": language}
        if api.call("getMyCommands", query) != commands:
            api.call("setMyCommands", {**query, "commands": commands})
        if api.call("getMyCommands", query) != commands:
            raise BridgeError("Telegram command menu verification failed")
    if api.call("getChatMenuButton", {"chat_id": chat_id}).get("type") != "commands":
        api.call("setChatMenuButton", {"chat_id": chat_id, "menu_button": {"type": "commands"}})
    if api.call("getChatMenuButton", {"chat_id": chat_id}).get("type") != "commands":
        raise BridgeError("Telegram menu button verification failed")
    return [command["command"] for command in commands]


def configure_presentation(api, inbox):
    """Apply this installation's name and menus; never change enrollment."""
    if api.call("getMe").get("username") != BOT:
        raise BridgeError("Bot identity mismatch")
    name = CHIEF_NICKNAME or CHIEF_NAME
    for language in ("", "en", "pt"):
        query = {"language_code": language}
        if api.call("getMyName", query).get("name") != name:
            api.call("setMyName", {**query, "name": name})
        if api.call("getMyName", query).get("name") != name:
            raise BridgeError("Telegram name verification failed")
    # Menus belong to enrolled private chats, never the public default scope.
    query = {"scope": {"type": "default"}}
    if api.call("getMyCommands", query):
        api.call("setMyCommands", {**query, "commands": []})
    menus = {}
    for user in inbox.db.execute("SELECT chat_id,label FROM users"):
        if user["label"] in INVITE_FILES:
            menus[user["label"]] = configure_chat_menu(api, user["chat_id"], user["label"])
    with inbox.db:
        inbox.set_meta("presentation_health", "verified")
    return {"display_name": name, "menus": menus, "verified": True}


def serve(inbox):
    lock = open(inbox.root / "collector.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise BridgeError("A collector is already running") from None
    api = Telegram(inbox.root)
    if api.call("getMe").get("username") != BOT:
        raise BridgeError("Bot identity mismatch")
    if api.call("getWebhookInfo").get("url"):
        raise BridgeError("Existing webhook found; no publishing route was changed")
    try:
        configure_presentation(api, inbox)
    except BridgeError:
        with inbox.db:
            inbox.set_meta("presentation_health", "setup_failed")
    from alfred_appserver import start_if_enabled
    try:
        adapter = start_if_enabled(inbox.root)
    except BridgeError:
        adapter = None
        with inbox.db:
            inbox.set_meta("appserver_health", "blocked_configuration")
    with inbox.db:
        inbox.set_meta("appserver_enabled", "1" if adapter else "0")
    from alfred_maintenance import Service as MaintenanceService
    from alfred_tasks import due_tasks
    from operator_policy import WORKSPACE
    maintenance = MaintenanceService(inbox.root, WORKSPACE, adapter.signal if adapter else None)
    stop = False
    def stopping(*_):
        nonlocal stop
        stop = True
        maintenance.stop()
        if adapter:
            adapter.stop()
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    try:
        while not stop:
            if adapter and not adapter.thread.is_alive() and inbox.meta("appserver_health") != "another_adapter_is_running":
                with inbox.db:
                    inbox.set_meta("health", "adapter_stopped")
                raise BridgeError("Adapter stopped; collector will restart")
            try:
                updates = api.call("getUpdates", {"offset": int(inbox.meta("offset", "0")), "timeout": 25,
                                                   "limit": 50, "allowed_updates": ["message", "edited_message"]})
                for update in updates:
                    inbox.ingest(update, api)
                    if adapter:
                        adapter.signal()
                try:
                    if due_tasks(inbox, WORKSPACE) and adapter:
                        adapter.signal()
                except (OSError, ValueError, sqlite3.Error):
                    with inbox.db:
                        inbox.set_meta("recurring_tasks_health", "queue_unavailable_retry_next_poll")
                inbox.flush(api)
                with inbox.db:
                    inbox.set_meta("last_poll", time.time())
                    inbox.set_meta("health", "online")
                    cutoff = time.time() - 30 * 86400
                    inbox.expire_completed(cutoff)
                    inbox.db.execute("DELETE FROM outbox WHERE status IN ('sent','suppressed') AND created<?", (cutoff,))
                    inbox.db.execute("DELETE FROM outbox_parts WHERE NOT EXISTS (SELECT 1 FROM outbox o WHERE o.id=outbox_id)")
                    inbox.db.execute("DELETE FROM invites WHERE expires<?", (time.time(),))
            except BridgeError as error:
                with inbox.db:
                    inbox.set_meta("health", str(error))
                logging.warning("%s", error)
                if "409" in str(error) or "401" in str(error):
                    if adapter:
                        adapter.stop()
                    return  # Do not fight another consumer or loop on an invalid credential.
                # A full pending outbox must be allowed to drain even when ingestion
                # rejected an update; its transaction leaves the update unacknowledged.
                if isinstance(error, ReplyQueueFull):
                    inbox.flush(api)
                for _ in range(15):
                    if stop:
                        break
                    time.sleep(1)
    finally:
        maintenance.stop()
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "invite", "inbox", "claim", "finish", "reopen", "status"))
    parser.add_argument("--id")
    parser.add_argument("--verified-not-routed", action="store_true")
    parser.add_argument("--reply-file", type=Path)
    parser.add_argument("--label", choices=tuple(INVITE_FILES), default=CONTACT_LABEL)
    args = parser.parse_args()
    inbox = Inbox()
    handler = RotatingFileHandler(inbox.root / "collector.log", maxBytes=128 * 1024, backupCount=1)
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    try:
        if args.command == "serve":
            serve(inbox)
        elif args.command == "invite":
            print(json.dumps({"private_invitation_file": inbox.invitation(args.label)}))
        elif args.command == "inbox":
            print(json.dumps(inbox.read(), ensure_ascii=False))
        elif args.command == "claim":
            if not args.id or not inbox.claim(args.id):
                raise BridgeError("Request was already claimed or does not exist")
            print("Claimed")
        elif args.command == "reopen":
            if not args.verified_not_routed or not args.id or not inbox.reopen(args.id):
                raise BridgeError("Confirm the request was not already routed before reopening it")
            print("Reopened")
        elif args.command == "finish":
            if not args.id or not args.reply_file:
                raise BridgeError("Request ID and reply file are required")
            inbox.finish(args.id, args.reply_file.read_text())
            print("Completed; reply saved for delivery")
        else:
            data = inbox.read()
            data["pending_count"] = len(data.pop("requests"))
            data["processing_count"] = len(data.pop("processing"))
            data["verified_users"] = inbox.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            print(json.dumps(data))
    except (BridgeError, OSError, sqlite3.Error) as error:
        safe = str(error) if isinstance(error, BridgeError) else type(error).__name__
        logging.warning("%s", safe)
        print(json.dumps({"error": safe}), file=sys.stderr)
        return 1
    finally:
        inbox.db.close()
    return 0


if __name__ == "__main__":
    # The optional adapter imports this module; reuse its live classes when run as a script.
    sys.modules.setdefault("telegram_bridge", sys.modules[__name__])
    raise SystemExit(main())
