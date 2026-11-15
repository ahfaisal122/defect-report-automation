"""Save one email, handed over by a mailbox reader such as UiPath, as a report file.

The file lands in the input folder in the same layout as the other .txt reports, so the
normal pipeline picks it up on the next run. Nothing here calls the model or decides
anything.
"""

import hashlib
import re
from datetime import datetime
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

MAX_BODY_CHARS = 200_000
LOOKS_LIKE_HTML = re.compile(r"<(html|body|div|p|br|span|table)\b", re.IGNORECASE)
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "table"}


class EmailRejected(ValueError):
    """The email cannot become a report (empty or far too long)."""


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextOnly()
    parser.feed(html)
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text)


def _one_line(value: str) -> str:
    """Header values must stay on one line, or they could fake extra header lines."""
    return " ".join(value.split())


def sender_allowed(sender: str, allowed: str) -> bool:
    """True when the sender's address is on the comma-separated list, or the list is empty."""
    wanted = {item.strip().lower() for item in allowed.split(",") if item.strip()}
    return not wanted or parseaddr(sender)[1].lower() in wanted


def save_email_report(
    input_dir: Path, sender: str, subject: str, body: str, sent: str = ""
) -> tuple[str, bool]:
    """Write the email as a report file. Returns (file name, whether it is new).

    The file name comes from the email's content, so the same email handed over twice
    gives the same file and is not stored again.
    """
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if LOOKS_LIKE_HTML.search(body):
        body = html_to_text(body)
    body = body.strip()
    if not body:
        raise EmailRejected("The email has no text.")
    if len(body) > MAX_BODY_CHARS:
        raise EmailRejected("The email is too long to be a defect report.")

    sender, subject, sent = _one_line(sender), _one_line(subject), _one_line(sent)
    identity = hashlib.sha256(f"{sender}\n{subject}\n{body}".encode()).hexdigest()
    path = input_dir / f"mail_{identity[:16]}.txt"
    if path.exists():
        return path.name, False

    sent = sent or datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M")
    input_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"From: {sender}\nSent: {sent}\nSubject: {subject}\n\n{body}\n", encoding="utf-8"
    )
    return path.name, True
