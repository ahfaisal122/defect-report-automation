from defect_automation.masking import find_names, mask_text, unmask

FORWARDED = """From: Kayleigh Schmiedecke <kayleigh.schmiedecke@factory.example>
To: quality@factory.example
Subject: FW: Leak on SG-2026-0096

Please review Hayley's report below. Thanks, Kayleigh

---------- Forwarded message ----------
From: Hayley Baker <hayley.baker@factory.example>
To: Kayleigh Schmiedecke <kayleigh.schmiedecke@factory.example>

Leak found on 2 castings EN-245-9144 for order SG-2026-0096 on 2026-10-30.

Best regards,
Hayley Baker
Phone: +49 30 5550 4921
"""


def test_names_emails_and_phone_are_masked() -> None:
    result = mask_text(FORWARDED)
    for secret in ("Kayleigh", "Schmiedecke", "Hayley", "Baker", "@", "5550"):
        assert secret not in result.text
    assert result.mapping["[PERSON_1]"] == "Kayleigh Schmiedecke"
    assert result.mapping["[PERSON_2]"] == "Hayley Baker"
    assert "[PERSON_2]'s report" in result.text


def test_business_identifiers_survive_masking() -> None:
    text = mask_text(FORWARDED).text
    for fact in ("SG-2026-0096", "EN-245-9144", "2026-10-30", "2 castings"):
        assert fact in text


def test_sign_off_name_without_header_is_found() -> None:
    assert find_names("Leak on 3 units.\n\nKind regards,\nOmar Haddad\n") == ["Omar Haddad"]


def test_sign_off_followed_by_ordinary_words_is_not_a_name() -> None:
    assert find_names("Thanks for the quick help with this.\n\nQuality Team") == []


def test_masking_is_deterministic_and_reversible() -> None:
    first, second = mask_text(FORWARDED), mask_text(FORWARDED)
    assert first.text == second.text
    assert unmask("[PERSON_2]", first.mapping) == "Hayley Baker"
    assert unmask(None, first.mapping) is None
