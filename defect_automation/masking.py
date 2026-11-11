"""Replace personal data with tokens before any text reaches the model or a log.

Names are found where emails reliably carry them: From/To/Cc lines (including those
inside forwarded blocks) and the line after a sign-off such as "Best regards,".
A name that appears only in running text, with no header or sign-off, is not caught.
That limit is documented in the README.

Masking is deterministic, so the token-to-name mapping can be rebuilt from the stored
raw report whenever a real name is needed. The mapping itself is never stored.
"""

import re
from dataclasses import dataclass, field
from email.utils import parseaddr

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?:\+|\b00)\d[\d ()/-]{6,}\d")
ADDRESS_HEADER = re.compile(r"^(?:From|To|Cc):\s*(.+)$", re.IGNORECASE | re.MULTILINE)
SIGN_OFF = re.compile(
    r"\b(?:best regards|kind regards|warm regards|regards|many thanks|thanks|thank you"
    r"|cheers|sincerely|best wishes|best|br|mfg)\b[,!.]?[ \t]*\n?[ \t]*([^\n]*)",
    re.IGNORECASE,
)
NAME = re.compile(r"^[A-ZÄÖÜ][\w'’-]+(?: [A-ZÄÖÜ][\w'’-]+){0,2}$")
NOT_A_NAME = {"quality", "team", "department", "all", "everyone", "production", "assembly"}
PERSON_TOKEN = re.compile(r"\[PERSON_\d+\]")


@dataclass
class MaskResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # token -> original value


def _looks_like_name(candidate: str) -> bool:
    if not NAME.match(candidate):
        return False
    return not any(word.lower() in NOT_A_NAME for word in candidate.split())


def find_names(text: str) -> list[str]:
    """Names in order of first discovery. Longer names absorb their own parts."""
    found: list[str] = []
    for match in ADDRESS_HEADER.finditer(text):
        name = parseaddr(match.group(1))[0].strip()
        if name and _looks_like_name(name):
            found.append(name)
    for match in SIGN_OFF.finditer(text):
        candidate = match.group(1).strip().rstrip(".,")
        if _looks_like_name(candidate):
            found.append(candidate)

    unique: list[str] = []
    for name in found:
        if name not in unique:
            unique.append(name)
    # "Kayleigh" is the same person as "Kayleigh Schmiedecke": keep only the full name.
    return [
        name
        for name in unique
        if not any(name != other and name in other.split() for other in unique)
    ]


def mask_text(text: str) -> MaskResult:
    mapping: dict[str, str] = {}
    names = find_names(text)

    emails: list[str] = []
    for address in EMAIL.findall(text):
        if address not in emails:
            emails.append(address)
    for index, address in enumerate(emails, start=1):
        token = f"[EMAIL_{index}]"
        mapping[token] = address
        text = text.replace(address, token)

    replacements: list[tuple[str, str]] = []
    for index, name in enumerate(names, start=1):
        token = f"[PERSON_{index}]"
        mapping[token] = name
        replacements.append((name, token))
        parts = name.split()
        if len(parts) > 1:
            replacements.extend((part, token) for part in parts if len(part) >= 3)
    for value, token in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", token, text)

    text = PHONE.sub("[PHONE]", text)
    return MaskResult(text=text, mapping=mapping)


def remask(value: str | None, mapping: dict[str, str]) -> str | None:
    """Put the tokens back, so text a reviewer saw unmasked is stored masked again."""
    if value is None:
        return None
    for token, original in sorted(mapping.items(), key=lambda pair: len(pair[1]), reverse=True):
        value = value.replace(original, token)
    return value


def unmask(value: str | None, mapping: dict[str, str]) -> str | None:
    if value is None:
        return None
    for token, original in mapping.items():
        value = value.replace(token, original)
    return value
