"""How well does data protection find what it must, and leave alone what it must not?

A labelled corpus of the text people actually paste into AI tools at a bank: support cases, log
lines, code, spreadsheets, emails. It plants valid IBANs (11 countries, correct check digits),
card numbers (Luhn-valid, real issuer prefixes), emails, phone numbers and secrets, next to the
look-alikes that cause false alarms: IBANs with wrong check digits, ISINs, order and transaction
numbers (some Luhn-valid by chance), UUIDs, timestamps, amounts, IP addresses and version
numbers. Every planted value is labelled, so precision and recall are exact.

The rules were developed against the first corpus. A second, held-out corpus uses formats and
wording written afterwards and never tuned on: German national phone numbers, dashed card numbers,
lower-case IBANs, other vendors' API keys, German-language templates. Its score is the honest one.

The corpora are synthetic: real customer data cannot be published. The detectors never see the labels.
"""

from __future__ import annotations

import random
import string
import uuid
from collections import Counter
from dataclasses import dataclass

from aigw import dlp

COUNTRIES = {
    "DE": "8n10n",
    "NL": "4a10n",
    "FR": "5n5n11c2n",
    "GB": "4a6n8n",
    "AT": "5n11n",
    "ES": "4n4n2n10n",
    "IT": "1a5n5n12c",
    "BE": "3n7n2n",
    "LU": "3n13c",
    "IE": "4a6n8n",
    "PL": "8n16n",
}


def _bban(fmt: str, rng: random.Random) -> str:
    out, num = [], ""
    for ch in fmt:
        if ch.isdigit():
            num += ch
            continue
        n = int(num)
        num = ""
        pool = {"n": string.digits, "a": string.ascii_uppercase, "c": string.ascii_uppercase + string.digits}[
            ch
        ]
        out.append("".join(rng.choice(pool) for _ in range(n)))
    return "".join(out)


def iban(rng: random.Random, valid: bool = True) -> str:
    cc = rng.choice(list(COUNTRIES))
    bban = _bban(COUNTRIES[cc], rng)
    digits = "".join(str(int(c, 36)) for c in bban + cc + "00")
    check = 98 - int(digits) % 97
    if not valid:
        check = (check + rng.randint(1, 9)) % 97 or 7
    s = f"{cc}{check:02d}{bban}"
    if rng.random() < 0.6:  # printed in groups of four, as on statements
        s = " ".join(s[i : i + 4] for i in range(0, len(s), 4))
    return s


def luhn_complete(prefix: str, length: int, rng: random.Random) -> str:
    body = prefix + "".join(rng.choice(string.digits) for _ in range(length - len(prefix) - 1))
    for d in string.digits:
        if dlp.luhn_valid(body + d):
            return body + d
    raise AssertionError("unreachable")


def card(rng: random.Random) -> str:
    prefix, length = rng.choice([("4", 16), ("51", 16), ("55", 16), ("2221", 16), ("37", 15), ("6011", 16)])
    n = luhn_complete(prefix, length, rng)
    if rng.random() < 0.5 and length == 16:
        n = " ".join(n[i : i + 4] for i in range(0, 16, 4))
    return n


def email(rng: random.Random) -> str:
    first = rng.choice(["anna", "lukas", "mia", "jonas", "sofia", "noah", "lea", "elias"])
    last = rng.choice(["schmidt", "mueller", "weber", "fischer", "becker", "wagner"])
    dom = rng.choice(["gmail.com", "web.de", "gmx.de", "example.org", "outlook.com", "t-online.de"])
    return f"{first}.{last}{rng.randint(1, 99)}@{dom}"


def phone(rng: random.Random) -> str:
    return rng.choice(
        [
            f"+49 30 {rng.randint(1000000, 99999999)}",
            f"+49 151 {rng.randint(1000000, 99999999)}",
            f"+44 20 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
            f"+33 1 {rng.randint(10, 99)} {rng.randint(10, 99)} {rng.randint(10, 99)} {rng.randint(10, 99)}",
            f"+49-89-{rng.randint(100000, 9999999)}",
        ]
    )


def secret(rng: random.Random) -> str:
    alnum = string.ascii_letters + string.digits
    return rng.choice(
        [
            "AKIA" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
            "sk-ant-api03-" + "".join(rng.choice(alnum) for _ in range(40)),
            "ghp_" + "".join(rng.choice(alnum) for _ in range(36)),
            "-----BEGIN RSA PRIVATE KEY-----",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            + "".join(rng.choice(alnum) for _ in range(30)),
        ]
    )


def decoy(rng: random.Random) -> str:
    """Things that look like regulated data and are not."""
    return rng.choice(
        [
            lambda: f"order {luhn_complete('4', 16, rng)}",  # Luhn-valid by chance, but an order
            lambda: f"transaction id {rng.randint(10**15, 10**16 - 1)}",
            lambda: iban(rng, valid=False),
            lambda: f"ISIN DE000{rng.randint(1000000, 9999999)}",
            lambda: str(uuid.UUID(int=rng.getrandbits(128))),
            lambda: f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:15:00Z",
            lambda: f"EUR {rng.randint(1, 999)}.{rng.randint(100, 999)},{rng.randint(10, 99)}",
            lambda: f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}",
            lambda: f"v{rng.randint(1, 9)}.{rng.randint(0, 30)}.{rng.randint(0, 99)}",
            lambda: f"ticket no. {rng.randint(100000, 999999)}",
            lambda: f"postcode {rng.randint(10115, 14199)} Berlin",
            lambda: f"build {rng.randint(10**11, 10**12)}",
        ]
    )()


TEMPLATES = [
    "Customer writes: please send the refund to {x}, thanks. Also see {d}.",
    "Support case 4711: account holder asks why {x} was charged twice ({d}).",
    "ERROR payment-service: rejected transfer for {x} ref={d}",
    "| name | value |\n| field | {x} |\n| other | {d} |",
    'def test_transfer():\n    account = "{x}"  # {d}\n    assert send(account)',
    "Hi team, can you check {x}? It came in with {d} this morning.",
    "Summarise this complaint: the client ({x}) says the interest was wrong for {d}.",
]
GENERATORS = {"iban": iban, "card": card, "email": email, "phone": phone, "secret": secret}


@dataclass
class Case:
    text: str
    planted: list[tuple[str, str]]  # (kind, value)


def corpus(n: int = 2000, seed: int = 11) -> list[Case]:
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        kind = rng.choice([*GENERATORS, None])  # one case in six holds only look-alikes
        value = GENERATORS[kind](rng) if kind else decoy(rng)
        text = rng.choice(TEMPLATES).format(x=value, d=decoy(rng))
        cases.append(Case(text, [(kind, value)] if kind else []))
    return cases


def evaluate(cases: list[Case], context: bool = True) -> dict:
    tp, fp, fn = Counter(), Counter(), Counter()
    false_alarms = []
    for c in cases:
        found = {(f.kind, f.value) for f in dlp.scan(c.text, context=context)}
        planted = set(c.planted)
        for k, _ in found & planted:
            tp[k] += 1
        for k, v in found - planted:
            fp[k] += 1
            if len(false_alarms) < 8:
                false_alarms.append(f"{k}: {v!r}")
        for k, _ in planted - found:
            fn[k] += 1
    per = {}
    for k in GENERATORS:
        p = tp[k] / (tp[k] + fp[k]) if tp[k] + fp[k] else 1.0
        r = tp[k] / (tp[k] + fn[k]) if tp[k] + fn[k] else 1.0
        per[k] = {
            "planted": tp[k] + fn[k],
            "found": tp[k],
            "false_alarms": fp[k],
            "precision": p,
            "recall": r,
        }
    total_tp, total_fp, total_fn = sum(tp.values()), sum(fp.values()), sum(fn.values())
    return {
        "cases": len(cases),
        "per_kind": per,
        "precision": total_tp / (total_tp + total_fp),
        "recall": total_tp / (total_tp + total_fn),
        "false_alarm_examples": false_alarms,
    }


# ---- held out: written after the rules were tuned, and never tuned on ---------------------------------------


def iban_heldout(rng: random.Random) -> str:
    s = iban(rng).replace(" ", "")
    return rng.choice([s.lower(), s, " ".join(s[i : i + 4] for i in range(0, len(s), 4))])


def card_heldout(rng: random.Random) -> str:
    prefix = rng.choice(["4", "53", "2720", "34"])
    n = luhn_complete(prefix, 15 if prefix == "34" else 16, rng)
    return rng.choice([n, "-".join(n[i : i + 4] for i in range(0, len(n), 4))])


def phone_heldout(rng: random.Random) -> str:
    return rng.choice(
        [
            f"(030) {rng.randint(1000000, 9999999)}",
            f"0151 {rng.randint(10000000, 99999999)}",
            f"+49 (0)30 {rng.randint(1000000, 9999999)}",
            f"+41 44 {rng.randint(100, 999)} {rng.randint(10, 99)} {rng.randint(10, 99)}",
            f"0049 30 {rng.randint(1000000, 9999999)}",
        ]
    )


def email_heldout(rng: random.Random) -> str:
    return rng.choice(
        [f"{email(rng).split('@')[0]}+bank@{d}" for d in ("posteo.de", "proton.me", "firma.co.uk")]
    )


def secret_heldout(rng: random.Random) -> str:
    alnum = string.ascii_letters + string.digits
    return rng.choice(
        [
            "AIza" + "".join(rng.choice(alnum + "-_") for _ in range(35)),  # Google API key
            "sk_live_" + "".join(rng.choice(alnum) for _ in range(24)),  # Stripe secret key
            "xoxb-"
            + "-".join(str(rng.randint(10**10, 10**11)) for _ in range(2))
            + "-"
            + "".join(rng.choice(alnum) for _ in range(24)),  # Slack bot token
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "ghs_" + "".join(rng.choice(alnum) for _ in range(36)),
        ]
    )


HELDOUT_TEMPLATES = [
    "Kunde bittet um Rückerstattung auf {x}. Vorgang {d}.",
    "Bitte prüfen: {x} wurde am {d} zweimal belastet.",
    "WARN reconciliation mismatch account={x} batch={d}",
    "config:\n  token: {x}\n  region: eu-central-1  # {d}",
    "Weiterleitung von Lisa: Erreichbar unter {x}, siehe {d}.",
]
HELDOUT = {
    "iban": iban_heldout,
    "card": card_heldout,
    "email": email_heldout,
    "phone": phone_heldout,
    "secret": secret_heldout,
}


def heldout_corpus(n: int = 1000, seed: int = 31) -> list[Case]:
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        kind = rng.choice([*HELDOUT, None])
        value = HELDOUT[kind](rng) if kind else decoy(rng)
        cases.append(
            Case(rng.choice(HELDOUT_TEMPLATES).format(x=value, d=decoy(rng)), [(kind, value)] if kind else [])
        )
    return cases


def run() -> dict:
    cases = corpus()
    return {
        "development": evaluate(cases, True),
        "development_without_context_rules": evaluate(cases, False),
        "held_out": evaluate(heldout_corpus(), True),
    }
