from __future__ import annotations

import pytest

from triage_kit.redact import placeholders_in, redact, restore


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("mail me at jo.doe+x@example.com please", "EMAIL", "jo.doe+x@example.com"),
        ("call +44 7700 900123 today", "PHONE", "+44 7700 900123"),
        ("call +1 202 555 0148.", "PHONE", "+1 202 555 0148"),
        ("card 4111 1111 1111 1111 was used", "CARD", "4111 1111 1111 1111"),
        ("card 5555-5555-5555-4444", "CARD", "5555-5555-5555-4444"),
        ("IBAN GB82 WEST 1234 5698 7654 32.", "IBAN", "GB82 WEST 1234 5698 7654 32"),
        ("IBAN DE89370400440532013000", "IBAN", "DE89370400440532013000"),
        ("from 198.51.100.23 at noon", "IP", "198.51.100.23"),
        ("Hi, my name is Daniel Okafor, call me", "NAME", "Daniel Okafor"),
        ("Help!\n\nKind regards,\nLiam O'Brien", "NAME", "Liam O'Brien"),
        ("Thanks!\n  Ana Lucia Costa  ", "NAME", "Ana Lucia Costa"),
        ("card 4111 1111 1111 1111 123 exp", "CARD", "4111 1111 1111 1111 123"),
        ("card 4111.1111.1111.1111", "CARD", "4111.1111.1111.1111"),
        ("write to jürgen.weiß@example.com", "EMAIL", "jürgen.weiß@example.com"),
        ("iban gb82 west 1234 5698 7654 32", "IBAN", "gb82 west 1234 5698 7654 32"),
        ("from 2001:db8::1 today", "IP", "2001:db8::1"),
        ("Thanks,\r\nSøren Ørsted\r\n", "NAME", "Søren Ørsted"),
        ("Help\n\nKind Regards,\nAna Lee", "NAME", "Ana Lee"),
        ("My name is Zoë Müller.", "NAME", "Zoë Müller"),
    ],
)
def test_detects(text: str, kind: str, value: str) -> None:
    r = redact(text)
    assert value not in r.text
    assert r.mapping == {f"[{kind}_1]": value}


@pytest.mark.parametrize(
    "text",
    [
        "order A-2291 and INV-88213",  # order numbers
        "card ending 4417",  # partial card
        "on 2026-11-08 at 09:00",  # dates and times
        "it cost 129.99, version 4.2.1",  # amounts and versions
        "call 555 0148",  # too few digits for a phone number
        "Best,\nOrder Number",  # not a name
        "screen burn-in",
        "at 12:30:45 and 09:40",  # times are not IPv6 addresses
    ],
)
def test_leaves_non_personal_data_alone(text: str) -> None:
    assert redact(text).mapping == {}


@pytest.mark.parametrize(
    ("text", "kind"),
    [("4111 1111 1111 1112", "CARD"), ("GB00 WEST 1234 5698 7654 32", "IBAN")],
)
def test_checksums_decide_card_and_iban(text: str, kind: str) -> None:
    # A failed checksum means "not a card/IBAN". A long digit run may still be redacted as a
    # phone number: over-redaction is the safe direction.
    assert not any(ph.startswith(f"[{kind}") for ph in redact(text).mapping)


def test_same_value_same_placeholder_and_restore() -> None:
    text = "a@example.com wrote; reply to a@example.com or b@example.org"
    r = redact(text)
    assert r.text == "[EMAIL_1] wrote; reply to [EMAIL_1] or [EMAIL_2]"
    assert restore(r.text, r.mapping) == text
    assert placeholders_in(r.text) == {"[EMAIL_1]", "[EMAIL_2]"}


def test_name_found_once_is_redacted_everywhere() -> None:
    r = redact("Priya Raman here, account of Priya Raman.\n\nThanks,\nPriya Raman")
    assert "Priya Raman" not in r.text
    assert r.counts() == {"NAME": 1}


def test_restore_leaves_unknown_placeholders() -> None:
    assert restore("to [EMAIL_9]", {"[EMAIL_1]": "x@example.com"}) == "to [EMAIL_9]"


def test_iban_followed_by_a_word_is_still_redacted() -> None:
    r = redact("IBAN GB82 WEST 1234 5698 7654 32 EUR please")
    assert r.text == "IBAN [IBAN_1] EUR please"


def test_restore_never_puts_back_cards_ibans_or_ips() -> None:
    r = redact("mail a@example.com, card 4111 1111 1111 1111, ip 192.0.2.7")
    assert restore(r.text, r.mapping) == "mail a@example.com, card [CARD_1], ip [IP_1]"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Groeten,\nMila van den Berg", "Mila van den Berg"),
        ("Met vriendelijke groet,\nSanne de Vries", "Sanne de Vries"),
        ("Mijn naam is Pieter Smit. De doos is kapot.", "Pieter Smit"),
        ("Hallo, ik ben Jan de Vries en ik heb een vraag.", "Jan de Vries"),
    ],
)
def test_dutch_names(text: str, name: str) -> None:
    r = redact(text)
    assert r.mapping == {"[NAME_1]": name}


def test_lower_case_words_after_ik_ben_are_not_names() -> None:
    assert redact("Ik ben blij met de service.").mapping == {}


@pytest.mark.parametrize(
    "text", ["Met vriendelijke groet,\nKlantenservice", "Kind regards,\nSupport team"]
)
def test_our_own_signatures_are_not_names(text: str) -> None:
    assert redact(text).mapping == {}
