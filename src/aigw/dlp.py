"""Data loss prevention: find regulated data in a prompt before it leaves the company.

Detectors are validated, not just pattern-matched: an IBAN must pass its country length and
ISO 7064 mod-97 check, a card number the Luhn check and a known issuer prefix. That is what keeps
order numbers, ISINs and timestamps from being redacted.

Findings are replaced by placeholders (``[IBAN_1]``) that are consistent within a request. The
model never sees the value; the gateway puts it back into the answer, so the user still gets a
coherent reply. Secrets are never sent anywhere: a request containing one is blocked.

Not covered: personal names and free-text addresses (they need a language model to find, which
would itself see the data). The limits are listed in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# data class of each detector: the policy decides what each class may be sent to
CLASSES = {
    "email": "personal",
    "phone": "personal",
    "iban": "financial",
    "card": "financial",
    "secret": "secret",
}

IBAN_LENGTHS = {
    "AD": 24,
    "AT": 20,
    "BE": 16,
    "BG": 22,
    "CH": 21,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FR": 27,
    "GB": 22,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
}

_IBAN = re.compile(r"\b([A-Za-z]{2}\d{2}(?:[ ]?[A-Za-z0-9]){10,30})\b")
_CARD = re.compile(r"(?<![\d+])(?<!\d[.-])(?:\d[ -]?){12,18}\d(?!\d|[.-]\d)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_NATIONAL = re.compile(r"(?<![\w+])(?:\(0\d{2,5}\)|0\d{2,5}|00\d{2}[ ]\d{1,5})[ /-]\d{5,9}(?!\w)")
_PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ ./-]?(?:\(0\))?[ ./-]?\d{1,5}(?:[ ./-]?\d{2,6}){1,4})(?!\w)")
_SECRETS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ID
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),  # private keys
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),  # model provider API keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),  # JWTs
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API keys
    re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}\b"),  # Stripe secret keys
]


def iban_valid(candidate: str) -> bool:
    s = candidate.replace(" ", "").upper()
    if IBAN_LENGTHS.get(s[:2]) != len(s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def card_issuer(digits: str) -> bool:
    """Known payment-card prefixes (Visa, Mastercard, Amex, Discover, JCB, Maestro, UnionPay)."""
    two, four = int(digits[:2]), int(digits[:4])
    return (
        digits[0] == "4"
        and len(digits) in (13, 16, 19)
        or 51 <= two <= 55
        and len(digits) == 16
        or 2221 <= four <= 2720
        and len(digits) == 16
        or two in (34, 37)
        and len(digits) == 15
        or (digits.startswith("6011") or digits.startswith("65"))
        and len(digits) in (16, 19)
        or 3528 <= four <= 3589
        and len(digits) in (16, 19)
        or two in (50, 56, 57, 58, 67)
        and 12 <= len(digits) <= 19
        or digits.startswith("62")
        and 16 <= len(digits) <= 19
    )


_NOT_CARD = re.compile(
    r"(order|invoice|ticket|tracking|reference|ref|transaction|txn|id|no|nr|number)\W{0,3}$", re.I
)
_CARD_WORDS = re.compile(r"card|karte|visa|mastercard|amex|pan\b", re.I)


def _not_a_card(text: str, start: int) -> bool:
    """One random 16-digit number in ten passes the Luhn check. An identifier label right before it
    ("order 4500...") and no card wording nearby says it is not a card number."""
    before = text[max(0, start - 24) : start]
    return bool(_NOT_CARD.search(before)) and not _CARD_WORDS.search(text[max(0, start - 60) : start])


@dataclass
class Finding:
    kind: str
    start: int
    end: int
    value: str

    @property
    def data_class(self) -> str:
        return CLASSES[self.kind]


def scan(text: str, context: bool = True) -> list[Finding]:
    found: list[Finding] = []

    def add(kind: str, start: int, end: int) -> None:
        if any(f.start < end and start < f.end for f in found):
            return  # overlaps a finding already made (secrets, then IBANs, win)
        found.append(Finding(kind, start, end, text[start:end]))

    for rx in _SECRETS:
        for m in rx.finditer(text):
            add("secret", m.start(), m.end())
    for m in _IBAN.finditer(text):
        raw = m.group(1)
        length = IBAN_LENGTHS.get(raw[:2].upper())
        if not length:
            continue
        count = 0  # the pattern is greedy: cut at the country's IBAN length
        for i, ch in enumerate(raw):
            count += ch != " "
            if count == length:
                end = m.start() + i + 1
                if iban_valid(text[m.start() : end]):
                    add("iban", m.start(), end)
                break
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if luhn_valid(digits) and card_issuer(digits) and not (context and _not_a_card(text, m.start())):
            add("card", m.start(), m.end())
    for m in _EMAIL.finditer(text):
        add("email", m.start(), m.end())
    for rx in (_PHONE, _PHONE_NATIONAL):
        for m in rx.finditer(text):
            if 8 <= len(re.sub(r"\D", "", m.group(0))) <= 15:
                add("phone", m.start(), m.end())
    return sorted(found, key=lambda f: f.start)


@dataclass
class Pseudonymiser:
    """Replaces findings with stable placeholders for one request, and puts them back after."""

    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> value
    by_value: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def placeholder(self, f: Finding) -> str:
        if f.value in self.by_value:
            return self.by_value[f.value]
        self.counts[f.kind] = self.counts.get(f.kind, 0) + 1
        ph = f"[{f.kind.upper()}_{self.counts[f.kind]}]"
        self.mapping[ph], self.by_value[f.value] = f.value, ph
        return ph

    def redact(self, text: str, findings: list[Finding]) -> str:
        out, last = [], 0
        for f in findings:
            out += [text[last : f.start], self.placeholder(f)]
            last = f.end
        out.append(text[last:])
        return "".join(out)

    def restore(self, text: str) -> str:
        for ph, value in self.mapping.items():
            text = text.replace(ph, value)
        return text


_PARTIAL = re.compile(r"\[[A-Z_0-9]*$")


class StreamRestorer:
    """Restores placeholders in a streamed answer, where one may arrive split across chunks."""

    def __init__(self, p: Pseudonymiser):
        self.p, self.pending = p, ""

    def feed(self, chunk: str) -> str:
        text = self.pending + chunk
        m = _PARTIAL.search(text)
        if m and self.p.mapping:
            self.pending, text = text[m.start() :], text[: m.start()]
        else:
            self.pending = ""
        return self.p.restore(text)

    def flush(self) -> str:
        out, self.pending = self.p.restore(self.pending), ""
        return out
