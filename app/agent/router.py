"""Intent router: fast, deterministic classification of every customer message.

It runs before the LLM and drives code-level decisions (automatic escalation,
out-of-scope short-circuit, language), so it must be cheap and predictable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.guardrails.injection import detect_injection

Intent = Literal[
    "policy_question",
    "account_lookup",
    "action_request",
    "complaint",
    "out_of_scope",
    "injection_suspected",
]
Sentiment = Literal["positive", "neutral", "negative", "very_negative"]


def _rx(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


HUMAN_RE = _rx(
    r"human",
    r"real person",
    r"live agent",
    r"(?:talk|speak|connect)\s+(?:to|with)\s+(?:a|an|"
    r"some)?\s*(?:person|agent|someone|executive|representative|manager|supervisor)",
    r"representative",
    r"supervisor",
    r"call me",
    r"customer care se baat",
    r"insaan",
    r"manager se baat",
    r"kisi se baat",
    r"agent se baat",
)
LEGAL_RE = _rx(
    r"consumer (?:court|forum|commission|helpline)",
    r"district commission",
    r"legal notice",
    r"lawyer",
    r"advocate",
    r"sue",
    r"court",
    r"police",
    r"fir (?:darj|file|lodge|karunga|karungi)",
    r"national consumer helpline",
    r"nch",
    r"1915",
    r"cyber ?crime",
    r"legal action",
    r"case (?:file|darj)",
)
NEGATIVE_RE = _rx(
    r"worst",
    r"pathetic",
    r"useless",
    r"ridiculous",
    r"fraud",
    r"scam",
    r"cheat(?:ed|ing)?",
    r"angry",
    r"furious",
    r"disgusting",
    r"terrible",
    r"horrible",
    r"unacceptable",
    r"fed up",
    r"hate",
    r"never (?:again|buying|order)",
    r"bakwas",
    r"bekaar",
    r"ghatiya",
    r"chor",
    r"dhokha",
    r"loot",
    r"shame",
    r"nonsense",
    r"frustrat\w*",
    r"annoy\w*",
    r"disappoint\w*",
    r"waste",
    r"kya bakwas",
)
ACTION_RE = _rx(
    r"cancel\w*",
    r"refund\w*",
    r"return\w*",
    r"money back",
    r"paisa wapas",
    r"paise wapas",
    r"wapas (?:karo|chahiye|do)",
    r"change (?:the |my )?(?:delivery |shipping )?address",
    r"update (?:the |my )?address",
    r"address (?:change|badal\w*|update)",
    r"deliver (?:it )?to",
    r"reimburse\w*",
)
LOOKUP_RE = _rx(
    r"where is",
    r"track\w*",
    r"status",
    r"kab (?:aayega|milega|tak)",
    r"my orders?",
    r"order details",
    r"delivered\?",
    r"shipped\?",
    r"kahan hai",
    r"not (?:yet )?received",
    r"didn'?t (?:get|receive)",
    r"nahi (?:mila|aaya)",
    r"awb",
    r"courier",
)
POLICY_RE = _rx(
    r"policy",
    r"how (?:many|long|do|can|much)",
    r"can i",
    r"do you",
    r"is there",
    r"what is",
    r"kitne din",
    r"kaise",
    r"gst",
    r"invoice",
    r"warranty",
    r"emi",
    r"cod",
    r"cash on delivery",
    r"shipping (?:charge|fee|cost)",
    r"exchange",
    r"return window",
    r"international",
)
COMPLAINT_RE = _rx(
    r"damaged",
    r"defective",
    r"broken",
    r"wrong (?:item|size|product|colour|color)",
    r"missing",
    r"torn",
    r"leak\w*",
    r"poor quality",
    r"not working",
    r"kharab",
    r"toota",
    r"galat",
)
OUT_OF_SCOPE_RE = _rx(
    r"poem",
    r"joke",
    r"weather",
    r"stock (?:price|market)",
    r"bitcoin",
    r"crypto",
    r"homework",
    r"write (?:me )?(?:a |an )?(?:essay|code|story|song)",
    r"recipe",
    r"cricket score",
    r"election",
    r"prime minister",
    r"translate",
    r"python",
    r"javascript",
    r"movie",
    r"girlfriend",
    r"boyfriend",
    r"horoscope",
)
DOMAIN_RE = _rx(
    r"order",
    r"ao-?\d{5}",
    r"refund",
    r"cancel",
    r"deliver\w*",
    r"acme",
    r"tent",
    r"jacket",
    r"shoe\w*",
    r"backpack",
    r"address",
    r"payment",
    r"upi",
)
HINGLISH_TOKENS = {
    "hai",
    "hain",
    "nahi",
    "nahin",
    "kya",
    "mera",
    "meri",
    "mere",
    "mujhe",
    "kab",
    "karo",
    "kar",
    "aap",
    "aapka",
    "tha",
    "thi",
    "wapas",
    "paisa",
    "paise",
    "kyun",
    "kyu",
    "bhai",
    "abhi",
    "jaldi",
    "chahiye",
    "gaya",
    "gayi",
    "raha",
    "rahi",
    "kaise",
    "kitne",
    "bhi",
    "toh",
    "yaar",
    "karna",
    "karwana",
    "hoga",
    "diya",
    "liya",
    "wala",
    "wali",
    "kuch",
    "matlab",
    "bataiye",
    "batao",
    "dijiye",
    "krdo",
    "kardo",
    "haan",
    "accha",
    "acha",
}


@dataclass
class Route:
    intent: Intent
    confidence: float
    sentiment: Sentiment
    wants_human: bool
    legal_threat: bool
    language: Literal["en", "hinglish"]
    signals: list[str] = field(default_factory=list)


def detect_language(text: str) -> Literal["en", "hinglish"]:
    tokens = re.findall(r"[a-z]+", text.lower())
    hits = sum(1 for t in tokens if t in HINGLISH_TOKENS)
    return "hinglish" if hits >= 2 or (tokens and hits / len(tokens) >= 0.25) else "en"


def detect_sentiment(text: str) -> Sentiment:
    negatives = len(NEGATIVE_RE.findall(text))
    letters = [c for c in text if c.isalpha()]
    shouting = len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.6
    exclaim = text.count("!") >= 3
    score = negatives + int(shouting) + int(exclaim)
    if score >= 2:
        return "very_negative"
    if score == 1:
        return "negative"
    if re.search(
        r"\b(?:thanks|thank you|great|awesome|perfect|dhanyavaad|shukriya)\b", text, re.IGNORECASE
    ):
        return "positive"
    return "neutral"


QUESTION_START_RE = re.compile(
    r"^\s*(?:how|what|when|can|could|do|does|is|are|will|which|kitne|kya|kaise)\b", re.IGNORECASE
)
ORDER_NO_RE = re.compile(r"\bAO-?\d{5}\b", re.IGNORECASE)


def _is_general_policy_question(text: str) -> bool:
    """'What is your return policy?' is a policy question, not a request to act."""
    asks = text.strip().endswith("?") or bool(QUESTION_START_RE.match(text))
    personal = re.search(r"\b(?:my|mera|meri|mere|mujhe|this order)\b", text, re.IGNORECASE)
    return asks and bool(POLICY_RE.search(text)) and not ORDER_NO_RE.search(text) and not personal


def classify(text: str) -> Route:
    signals: list[str] = []
    sentiment = detect_sentiment(text)
    wants_human = bool(HUMAN_RE.search(text))
    legal = bool(LEGAL_RE.search(text))
    language = detect_language(text)
    injection = detect_injection(text)
    if wants_human:
        signals.append("wants_human")
    if legal:
        signals.append("legal_threat")

    intent: Intent
    if injection.suspected:
        intent, conf = "injection_suspected", injection.score
        signals.extend(injection.labels)
    elif OUT_OF_SCOPE_RE.search(text) and not DOMAIN_RE.search(text):
        intent, conf = "out_of_scope", 0.9
    elif ACTION_RE.search(text) and not _is_general_policy_question(text):
        intent, conf = "action_request", 0.8
    elif COMPLAINT_RE.search(text) or sentiment in ("negative", "very_negative"):
        intent, conf = "complaint", 0.7
    elif LOOKUP_RE.search(text) or ORDER_NO_RE.search(text):
        intent, conf = "account_lookup", 0.7
    elif POLICY_RE.search(text):
        intent, conf = "policy_question", 0.7
    else:
        intent, conf = "account_lookup" if DOMAIN_RE.search(text) else "policy_question", 0.4
    return Route(intent, conf, sentiment, wants_human, legal, language, signals)
