from email.message import EmailMessage

from defect_automation.parsing import clean_body, parse_report
from tests.conftest import SAMPLE_TXT


def test_txt_headers_and_body_are_split() -> None:
    parsed = parse_report("txt", SAMPLE_TXT)
    assert parsed.sender == "Anna Keller <anna.keller@factory.example>"
    assert parsed.subject == "Scratched gaskets on SG-2026-0003"
    assert parsed.sent == "Tue, 04 Aug 2026 09:15"
    assert parsed.body.startswith("Hello Quality,")
    assert "Subject:" not in parsed.body


def test_txt_without_headers_is_all_body() -> None:
    parsed = parse_report("txt", "Found 3 dented covers at final inspection.\nPlease check.")
    assert parsed.sender == ""
    assert parsed.body == "Found 3 dented covers at final inspection.\nPlease check."


def test_eml_is_decoded() -> None:
    message = EmailMessage()
    message["From"] = "Jörg Müller <joerg.mueller@factory.example>"
    message["To"] = "quality@factory.example"
    message["Subject"] = "Leak on SG-2026-0003"
    message.set_content("Hi,\n\nLeak at the flange seal, größer than expected.\n\nJörg\n")
    parsed = parse_report("eml", message.as_bytes().decode("utf-8"))
    assert parsed.sender.startswith("Jörg Müller")
    assert parsed.subject == "Leak on SG-2026-0003"
    assert "größer than expected" in parsed.body


def test_disclaimer_and_quoted_reply_are_removed() -> None:
    body = (
        "Leak found on 2 units.\n\n"
        "Regards,\nAnna\n"
        "This email is confidential and intended solely for the addressee.\n\n"
        "On Mon, 3 Aug 2026 at 10:00, Someone wrote:\n"
        "> old text\n> more old text"
    )
    assert clean_body(body) == "Leak found on 2 units.\n\nRegards,\nAnna"


def test_signature_block_is_dropped_but_forward_is_kept() -> None:
    body = (
        "Please see below.\n"
        "-- \n"
        "Anna Keller\nHead of Something\n"
        "---------- Forwarded message ----------\n"
        "From: Ben Vogt <ben.vogt@factory.example>\n\n"
        "Flashover on 1 pole."
    )
    cleaned = clean_body(body)
    assert "Head of Something" not in cleaned
    assert "Forwarded message" in cleaned
    assert "Flashover on 1 pole." in cleaned
