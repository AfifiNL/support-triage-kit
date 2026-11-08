"""A deterministic keyword model. It lets the pipeline, the gates, the API and the evals run
offline at zero cost, and gives the eval gate a stable baseline. It is not a good classifier;
the live run in the README measures a real model."""

from __future__ import annotations

import re

from triage_kit.models import Category, ModelOutput, Priority
from triage_kit.providers.base import TriageRequest

# English and Dutch keywords per category, more specific categories first.
KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.PRIVACY: (
        "personal data",
        "my data",
        "gdpr",
        "data protection",
        "erase",
        "persoonsgegevens",
        "avg",
        "inzage",
    ),
    Category.CANCELLATION: (
        "cancel",
        "unsubscribe",
        "terminate",
        "end my subscription",
        "stop my subscription",
        "close my subscription",
        "opzeggen",
        "annuleer",
        "annuleren",
    ),
    Category.BILLING: (
        "charged",
        "charge",
        "invoice",
        "refund",
        "payment",
        "billing",
        "paid",
        "vat",
        "receipt",
        "price",
        "card",
        "betaald",
        "betaling",
        "factuur",
        "afgeschreven",
        "btw",
        "bedrag",
    ),
    Category.SHIPPING: (
        "delivery",
        "deliver",
        "parcel",
        "package",
        "tracking",
        "shipped",
        "courier",
        "arrived",
        "damaged",
        "missing item",
        "pakket",
        "bezorger",
        "bezorging",
        "levering",
        "doos",
        "rugzak",
        "verstuurd",
        "track and trace",
    ),
    Category.ACCOUNT: (
        "password",
        "log in",
        "login",
        "sign in",
        "locked out",
        "two-factor",
        "2fa",
        "username",
        "email address",
        "account",
        "inloggen",
        "wachtwoord",
    ),
    Category.TECHNICAL: (
        "error",
        "crash",
        "bug",
        "not working",
        "doesn't work",
        "won't load",
        "broken",
        "app",
        "sync",
        "outage",
        "down",
        "slow",
        "500",
        "crasht",
        "storing",
        "fout",
        "synchroniseren",
        "oplader",
    ),
    Category.FEEDBACK: (
        "suggestion",
        "love",
        "great",
        "thank you",
        "feature request",
        "idea",
        "feedback",
        "compliment",
        "disappointed",
        "suggestie",
        "vriendelijk",
        "top service",
    ),
}
URGENT = (
    "outage",
    "is down",
    "all users",
    "nobody can",
    "production",
    "charged three times",
    "charged 3 times",
    "every hour",
    "storing",
    "ligt stil",
    "niemand in ons team",
)
HIGH = (
    "urgent",
    "asap",
    "today",
    "immediately",
    "charged twice",
    "double charged",
    "locked out",
    "can't access",
    "cannot access",
    "deadline",
    "still not",
    "dringend",
    "vandaag",
    "niet meer",
    "twee keer",
    "kan zo niet werken",
)
LOW = (
    "suggestion",
    "feature request",
    "idea",
    "just wanted",
    "love",
    "compliment",
    "no rush",
    "suggestie",
    "geen haast",
    "even laten weten",
)

REPLIES: dict[Category, str] = {
    Category.BILLING: "Thank you for contacting us about your payment. We are checking the "
    "charges on your account and will get back to you with what we find.",
    Category.TECHNICAL: "Thank you for reporting this problem. Our technical team is looking "
    "into it. If you can, please send a screenshot of the error and the steps that led to it.",
    Category.ACCOUNT: "Thank you for getting in touch about your account. For your security we "
    "will verify the request first and then help you regain access.",
    Category.SHIPPING: "Thank you for letting us know about your delivery. We are checking the "
    "status with the carrier and will update you as soon as we know more.",
    Category.CANCELLATION: "We have received your cancellation request. A colleague will confirm "
    "the details and the end date with you.",
    Category.PRIVACY: "We have received your request about your personal data. Our privacy team "
    "will handle it and contact you to confirm your identity first.",
    Category.FEEDBACK: "Thank you for your feedback. We have shared it with the team.",
    Category.OTHER: "Thank you for your message. A colleague will read it and get back to you.",
}


REPLIES_NL: dict[Category, str] = {
    Category.BILLING: "Bedankt voor uw bericht over uw betaling. We controleren de afschrijvingen "
    "op uw account en laten u weten wat we vinden.",
    Category.TECHNICAL: "Bedankt voor het melden van dit probleem. Ons technische team bekijkt "
    "het. Stuur ons als dat kan een schermafbeelding van de foutmelding.",
    Category.ACCOUNT: "Bedankt voor uw bericht over uw account. Voor uw veiligheid controleren we "
    "eerst het verzoek en helpen we u daarna weer toegang te krijgen.",
    Category.SHIPPING: "Bedankt voor uw bericht over de bezorging. We vragen de status na bij de "
    "vervoerder en laten u weten wat we horen.",
    Category.CANCELLATION: "We hebben uw verzoek om op te zeggen ontvangen. Een collega bevestigt "
    "de details en de einddatum met u.",
    Category.PRIVACY: "We hebben uw verzoek over uw persoonsgegevens ontvangen. Ons privacyteam "
    "neemt het in behandeling en vraagt u eerst om uw identiteit te bevestigen.",
    Category.FEEDBACK: "Bedankt voor uw reactie. We hebben die met het team gedeeld.",
    Category.OTHER: "Bedankt voor uw bericht. Een collega leest het en neemt contact met u op.",
}


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words)


class MockTriageModel:
    name = "mock-keywords"

    def triage(self, request: TriageRequest) -> ModelOutput:
        text = f"{request.subject}\n{request.body}".lower()
        scores = {
            cat: sum(1 for w in words if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text))
            for cat, words in KEYWORDS.items()
        }
        best = max(scores.values())
        # Ties resolve in KEYWORDS order, which lists the more specific categories first.
        category = next(c for c, s in scores.items() if s == best) if best > 0 else Category.OTHER
        if _has(text, URGENT):
            priority = Priority.URGENT
        elif _has(text, HIGH) or (
            request.customer_tier == "business" and category is Category.TECHNICAL
        ):
            priority = Priority.HIGH
        elif category is Category.FEEDBACK or _has(text, LOW):
            priority = Priority.LOW
        else:
            priority = Priority.NORMAL
        first_line = (request.subject.strip() or request.body.strip() or "(empty)").splitlines()[0]
        dutch = request.language == "nl"
        reply = (REPLIES_NL if dutch else REPLIES)[category]
        if category is Category.ACCOUNT and "[EMAIL_1]" in request.placeholders:
            reply += (
                " Een eventuele resetlink gaat naar [EMAIL_1]."
                if dutch
                else " Any reset link will go to [EMAIL_1]."
            )
        greeting, signature = ("Hallo,", "Klantenservice") if dutch else ("Hello,", "Support team")
        return ModelOutput(
            category=category,
            priority=priority,
            summary=f"{category.value}: {first_line[:120]}",
            reply=f"{greeting}\n\n{reply}\n\n{signature}",
        )
