from __future__ import annotations

import pytest

from triage_kit.language import detect


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("My parcel has not arrived yet, can you check?", "en"),
        ("Mijn pakket is nog niet aangekomen, kunt u het nakijken?", "nl"),
        ("Storing: niemand in ons team kan het dashboard openen.", "nl"),
        ("Outage: nobody on our team can open the dashboard.", "en"),
        ("", "en"),
    ],
)
def test_detect(text: str, lang: str) -> None:
    assert detect(text) == lang
