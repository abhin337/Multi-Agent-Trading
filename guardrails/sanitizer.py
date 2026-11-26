"""Prompt-injection and adversarial payload sanitizer for untrusted market news."""

from __future__ import annotations

import base64
import re

from schemas.models import SanitizedText

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_BASE64_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/]{20,}={0,2}\b")

_INJECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "hitl_override_attempt",
        re.compile(
            r"\b(?:system\s+override|pre-?approved\s+this\s+trade|"
            r"skip\s+(?:the\s+)?hitl(?:\s+gate)?|bypass\s+hitl)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_exfiltration_attempt",
        re.compile(
            r"\b(?:send|exfiltrate|print|reveal|dump|return)\b.{0,40}"
            r"\b(?:zerodha|kite|api[_\s-]?key|access[_\s-]?token|auth(?:orization)?\s+header|session)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "fake_compliance_liquidation",
        re.compile(
            r"\b(?:compliance\s+alert|regulatory\s+mandate|urgent\s+liquidation)\b.{0,60}"
            r"\b(?:liquidate|sell\s+all|exit\s+all)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ticker_substitution_attempt",
        re.compile(
            r"\b(?:substitute\s+ticker|replace\s+ticker|final\s+order\s+(?:for|using)\s+ticker|"
            r"buy\s+\d+\s+shares\s+of\s+[A-Z]{2,15}\s+at\s+market)\b",
            re.IGNORECASE,
        ),
    ),
]

_DECODED_SUSPICIOUS_KEYWORDS = (
    "ignore",
    "override",
    "system",
    "hitl",
    "buy ",
    "sell ",
    "liquidate",
    "kite",
    "zerodha",
    "api_key",
    "token",
)


def _try_detect_encoded_injection(blob: str) -> bool:
    """Return True if a base64-like token decodes into suspicious directive text."""
    try:
        padded = blob + "=" * (-len(blob) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="ignore").lower()
    except Exception:
        return False
    if not decoded.strip():
        return False
    return any(keyword in decoded for keyword in _DECODED_SUSPICIOUS_KEYWORDS)


def sanitize_untrusted_text(raw_text: str) -> SanitizedText:
    """Strip hidden markup and neutralize prompt-injection directives in external text."""
    matched_rules: list[str] = []
    text = raw_text

    if _HTML_COMMENT_RE.search(text):
        matched_rules.append("html_comment_stripped")
        text = _HTML_COMMENT_RE.sub(" [REDACTED_HTML_COMMENT] ", text)

    if _SCRIPT_TAG_RE.search(text):
        matched_rules.append("script_tag_stripped")
        text = _SCRIPT_TAG_RE.sub(" [REDACTED_SCRIPT] ", text)

    def _replace_b64(match: re.Match[str]) -> str:
        token = match.group(0)
        if _try_detect_encoded_injection(token):
            if "base64_encoded_override" not in matched_rules:
                matched_rules.append("base64_encoded_override")
            return "[REDACTED_ENCODED_PAYLOAD]"
        return token

    text = _BASE64_BLOB_RE.sub(_replace_b64, text)

    for rule_name, pattern in _INJECTION_RULES:
        if pattern.search(text):
            if rule_name not in matched_rules:
                matched_rules.append(rule_name)
            text = pattern.sub("[REDACTED_UNTRUSTED_DIRECTIVE]", text)

    cleaned = re.sub(r"\s+", " ", text).strip()
    return SanitizedText(
        cleaned_text=cleaned,
        injection_detected=len(matched_rules) > 0,
        matched_rules=matched_rules,
    )
