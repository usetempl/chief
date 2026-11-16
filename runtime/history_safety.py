"""Conservative transcript filtering; never a guarantee that all secrets are detected."""
import re
from operator_policy import SENSITIVE_REQUEST_IDS

# Short device-login values can look like ordinary words or identifiers. Require
# an authentication label or a dedicated URL parameter before treating them as codes.
AUTH_VALUE = (r"(?<![A-Za-z0-9])(?:[0-9]{4,10}|[A-Z0-9]{3,}(?:-[A-Z0-9]{3,})+|"
              r"[a-z]{2,}(?:-[a-z]{2,}){3,5}|[A-Z0-9]{6,12}|"
              r"(?=[A-Za-z0-9]{6,12}\b)(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{6,12})"
              r"(?![A-Za-z0-9_-])")
AUTH_LABEL = (r"(?i:\b(?:device(?:[ -]+authori[sz]ation)?|pairing|login|sign[ -]?in|"
              r"authori[sz]ation|authentication|verification|one[ -]?time(?:[ -]+authentication)?)"
              r"[ -]+codes?\b)")
AUTH_LINKED_LABEL = (
    r"(?i:\bdevice[ -]+(?:sign[ -]?in|login)\b|https?://github\.com/login/device\b)"
    r"[\s\S]{0,160}?(?i:\bcodes?\b)"
)
AUTH_CODE = re.compile(
    r"(?:" + AUTH_LABEL + r"|" + AUTH_LINKED_LABEL + r")" + r"[\s\S]{0,120}?" + AUTH_VALUE +
    r"|(?i:[?&](?:user_code|device_code|authorization_code|login_code|one_time_code|otp)=)"
    r"[A-Za-z0-9%][A-Za-z0-9._~%+-]*"
)

PATTERNS = [
    ("authentication_code", AUTH_CODE.pattern),
    ("telegram_bot_token", r"(?<![A-Za-z0-9_])\d{5,16}:[A-Za-z0-9_-]{30,64}(?![A-Za-z0-9_-])"),
    ("telegram_invite", r"(?i)(https://t\.me/[A-Za-z0-9_]+\?start=)[A-Za-z0-9_-]{20,64}"),
    ("private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"),
    ("provider_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"),
    ("storage_credential", r"(?im)\b(?:s3|r2|aws)[_ -]+(?:access[_ -]+key(?:[_ -]+id)?|key|secret(?:[_ -]+(?:access[_ -]+)?key)?)\b[\"']?\s*[:=]\s*[^\n]+"),
    ("credential_assignment", r"(?im)\b(?:password|passwd|api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|secret[_ -]?key|otp|one[_ -]?time[_ -]?code)\b[\"']?\s*[:=]\s*[^\n]+"),
    ("natural_credential", r"(?im)\b(?:password|passwd|api[_ -]?key|private[_ -]?key|seed phrase|recovery phrase|mnemonic|OTP|one.time code)\b.{0,24}\b(?:is|was|here|use)\b[^\n]+"),
    ("authorization", r"(?im)\b(?:authorization|cookie|set-cookie)\s*:\s*[^\n]+"),
    ("bearer", r"\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    ("url_credentials", r"https?://[^\s/@]+:[^\s/@]+@[^\s]+"),
    ("url_token", r"(?i)(?:[?&](?:token|key|secret|password|signature|access_token)=)[^\s&#]+"),
    ("email", r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("long_token", r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9])"),
]
COMPILED = [(name, re.compile(pattern)) for name, pattern in PATTERNS]
# Exclude entire messages that discuss potentially pasted credential material.
# Some technical context is intentionally lost; do not publish ambiguous secrets.
SENSITIVE_CONTEXT = re.compile(
    r"(?i)\b(?:password|passwd|passphrase|mnemonic|seed phrase|recovery phrase|"
    r"private key|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
    r"OTP|one.time (?:code|password))\b|"
    r"\b(?:s3|r2|aws)[_ -]+(?:access[_ -]+key(?:[_ -]+id)?|key|"
    r"secret(?:[_ -]+(?:access[_ -]+)?key)?)\b[\"']?\s*[:=]"
)


def redact(text, conservative=False):
    if conservative and re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", text.strip()):
        return "[Message omitted: potentially sensitive authentication context]", {"authentication_message": 1}
    if conservative and any('"request_id": "' + key + '"' in text for key in SENSITIVE_REQUEST_IDS):
        return "[Message omitted: potentially sensitive authentication context]", {"authentication_message": 1}
    if conservative and AUTH_CODE.search(text):
        return "[Message omitted: potentially sensitive authentication context]", {"authentication_message": 1}
    if conservative and SENSITIVE_CONTEXT.search(text):
        return "[Message omitted: potentially sensitive credential context]", {"sensitive_message": 1}
    counts = {}
    for name, pattern in COMPILED:
        text, count = pattern.subn("[REDACTED]", text)
        if count:
            counts[name] = count
    return text, counts


def matches(text, transcript=False):
    # Documentation/manifests intentionally contain long checkout paths and SHA hashes.
    return [name for name, pattern in COMPILED
            if (transcript or name != "long_token") and pattern.search(text)]
