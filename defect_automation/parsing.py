"""Turn a raw .txt or .eml report into headers plus a cleaned body.

Cleaning removes text that carries no defect facts: legal disclaimers, signature blocks
behind a "-- " delimiter and quoted replies. Forwarded messages are kept, because the
original report usually sits inside them.
"""

import email
import email.policy
import re
from dataclasses import dataclass

HEADER_LINE = re.compile(r"^(From|To|Cc|Sent|Date|Subject):\s*(.*)$", re.IGNORECASE)
DISCLAIMER = re.compile(
    r"confidential|intended (solely|only) for|received this (e-?mail|message) in error"
    r"|disseminate|legally privileged|not the (named|intended) (addressee|recipient)",
    re.IGNORECASE,
)
QUOTE_INTRO = re.compile(r"^On .{5,100} wrote:\s*$")
FORWARD_MARKER = re.compile(r"^-{2,}\s*(Forwarded|Original) message\s*-{2,}", re.IGNORECASE)
SIGNATURE_DELIMITER = re.compile(r"^--\s?$")


@dataclass(frozen=True)
class ParsedReport:
    sender: str
    recipient: str
    sent: str
    subject: str
    body: str

    def as_text(self) -> str:
        """The text the model sees (after masking): a small header block, then the body."""
        return (
            f"From: {self.sender}\nTo: {self.recipient}\nSent: {self.sent}\n"
            f"Subject: {self.subject}\n\n{self.body}"
        )


def _parse_eml(raw_text: str) -> ParsedReport:
    # Parse from bytes: the string parser loses non-ASCII characters in 8-bit bodies.
    message = email.message_from_bytes(raw_text.encode("utf-8"), policy=email.policy.default)
    part = message.get_body(preferencelist=("plain",))
    body = part.get_content() if part is not None else ""
    return ParsedReport(
        sender=str(message.get("From", "")),
        recipient=str(message.get("To", "")),
        sent=str(message.get("Date", "")),
        subject=str(message.get("Subject", "")),
        body=body,
    )


def _parse_txt(raw_text: str) -> ParsedReport:
    """Read an optional leading block of 'Key: value' lines. Without one, all text is body."""
    lines = raw_text.splitlines()
    headers: dict[str, str] = {}
    index = 0
    while index < len(lines) and (match := HEADER_LINE.match(lines[index])):
        headers[match.group(1).lower()] = match.group(2).strip()
        index += 1
    return ParsedReport(
        sender=headers.get("from", ""),
        recipient=headers.get("to", ""),
        sent=headers.get("sent", headers.get("date", "")),
        subject=headers.get("subject", ""),
        body="\n".join(lines[index:]),
    )


def clean_body(body: str) -> str:
    kept: list[str] = []
    in_signature = False
    for line in body.splitlines():
        stripped = line.strip()
        if FORWARD_MARKER.match(stripped):
            in_signature = False
        elif SIGNATURE_DELIMITER.match(line):
            in_signature = True
        if in_signature:
            continue
        if stripped.startswith(">") or QUOTE_INTRO.match(stripped) or DISCLAIMER.search(stripped):
            continue
        kept.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def parse_report(source_type: str, raw_text: str, clean: bool = True) -> ParsedReport:
    """clean=False keeps the body as written, for showing the original to a reviewer."""
    parsed = _parse_eml(raw_text) if source_type == "eml" else _parse_txt(raw_text)
    if not clean:
        return parsed
    return ParsedReport(
        sender=parsed.sender,
        recipient=parsed.recipient,
        sent=parsed.sent,
        subject=parsed.subject,
        body=clean_body(parsed.body),
    )
