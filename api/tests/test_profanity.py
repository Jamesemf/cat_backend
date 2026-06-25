"""Display-name profanity filter: catch evasions, spare innocent names."""

import pytest

from app.utils.profanity import contains_profanity


@pytest.mark.parametrize(
    "name",
    [
        "fuck",
        "FUCK",
        "F.U.C.K",
        "f u c k",
        "Sh1t",
        "b1tch",
        "a$$",
        "n1gger",
        "n igga",
        "Mr Twat",
        "ass",          # whole-word
        "damn it",
        "Pussy",        # standalone is profane even though "Pussycat" is fine
    ],
)
def test_blocks_profanity_and_evasions(name):
    assert contains_profanity(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "James",
        "Cassidy",       # contains "ass"
        "Glass Cat",
        "Scunthorpe",    # contains "cunt"
        "analysis",      # contains "anal"
        "raccoon",       # contains "coon"
        "Clemens",       # contains "semen"
        "title",         # contains "tit"
        "Sexton",        # contains "sex"
        "Therapist",     # contains "rapist"
        "Pussycat",      # cat app: explicitly allowed
        "Shiitake",      # contains "shit"
        "Assassin",
        "Constitution",
    ],
)
def test_allows_innocent_names(name):
    assert contains_profanity(name) is False


def test_empty_and_none_are_clean():
    assert contains_profanity(None) is False
    assert contains_profanity("") is False
    assert contains_profanity("   ") is False
