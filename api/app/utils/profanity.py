"""Display-name profanity filter.

A single source of truth for forbidden words in user-chosen names. Pure
helpers (no FastAPI dependency) so it can be reused by any router or service.

Matching strategy
-----------------
Names are normalised before comparison so common evasions are caught:

* case is folded (``FUCK`` == ``fuck``)
* leetspeak is undone (``fvck`` no, but ``f0ck``, ``$h1t`` yes)
* separators are stripped so spaced/punctuated spellings collapse
  (``f.u.c.k``, ``s h i t`` -> ``fuck``, ``shit``)

Two tiers of terms:

* :data:`SUBSTRING_TERMS` match anywhere in the collapsed name. These are long
  enough that they rarely appear inside an innocent word.
* :data:`WHOLE_WORD_TERMS` match only as a standalone word, so short or
  embeddable fragments (``ass``, ``anal``, ``sex``) don't trip on ``Cassidy``,
  ``analysis`` or ``Sexton``.

:data:`ALLOWLIST` rescues the handful of legitimate words that still collide
with a substring term (the "Scunthorpe problem"): an allowlisted word is
dropped before substring matching runs.

Tuning the balance: prefer blocking over the rare false positive, but if a
common name gets caught, either move the offending term to ``WHOLE_WORD_TERMS``
or add the name to ``ALLOWLIST``.
"""

import re

# Leetspeak / lookalike substitutions applied during normalisation.
_LEET = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "i",
        "*": "",
    }
)

# Blocked anywhere in the collapsed name. Curated so they don't appear inside
# ordinary words. Keep alphabetical for easy maintenance.
SUBSTRING_TERMS: frozenset[str] = frozenset(
    {
        "ballsack",
        "bastard",
        "bellend",
        "bitch",
        "blowjob",
        "bollock",
        "boner",
        "buttplug",
        "chink",
        "clitoris",
        "cocksucker",
        "cunt",
        "dickhead",
        "dildo",
        "douchebag",
        "dyke",
        "ejaculate",
        "faggot",
        "fellatio",
        "fuck",
        "gangbang",
        "handjob",
        "homosexual",
        "jizz",
        "kike",
        "knob",
        "kunt",
        "labia",
        "molest",
        "motherfucker",
        "nazi",
        "negro",
        "nigga",
        "nigger",
        "nonce",
        "paedo",
        "pedophile",
        "phuck",
        "pussy",
        "queef",
        "rapist",
        "retard",
        "rimjob",
        "scrotum",
        "shit",
        "shite",
        "slut",
        "smegma",
        "spastic",
        "spunk",
        "testicle",
        "twat",
        "vagina",
        "wank",
        "whore",
    }
)

# Short or embeddable terms: only blocked when they stand alone as a word, so
# they don't trip on innocent names (``ass`` in "Cassidy", "anal" in
# "analysis", "coon" in "raccoon", "tit" in "title").
WHOLE_WORD_TERMS: frozenset[str] = frozenset(
    {
        "anal",
        "anus",
        "arse",
        "ass",
        "cock",
        "coon",
        "cum",
        "damn",
        "dick",
        "fag",
        "fuk",
        "gay",
        "hell",
        "homo",
        "minge",
        "penis",
        "piss",
        "prick",
        "rape",
        "raped",
        "rapes",
        "raping",
        "semen",
        "sex",
        "slag",
        "tit",
        "tits",
        "wtf",
    }
)

# Legitimate words that contain a substring term — dropped before matching so
# they aren't blocked. Pussy(cat/willow) earns its place in a cat app.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "pussycat",
        "pussycats",
        "pussywillow",
        "scunthorpe",
        "shiitake",
        "shitake",
        "therapist",
        "therapists",
    }
)


def contains_profanity(name: str | None) -> bool:
    """True if ``name`` contains a forbidden word under fuzzy matching."""
    if not name:
        return False

    folded = name.lower().translate(_LEET)
    words = re.findall(r"[a-z]+", folded)

    if set(words) & WHOLE_WORD_TERMS:
        return True

    # Collapse the remaining words into one run so spaced/punctuated evasions
    # ("f u c k", "n.i.g.g.a") are caught, while allowlisted words drop out.
    collapsed = "".join(w for w in words if w not in ALLOWLIST)
    return any(term in collapsed for term in SUBSTRING_TERMS)
