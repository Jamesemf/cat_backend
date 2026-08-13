"""Ownership-claim intake.

Claims used to be decided here, by scoring owner-submitted photos against the
cat's stored vision features. They aren't any more: ownership is granted by a
human in /moderation/claims, and nothing in this module decides anything.

What survives is intake — the limits on how often someone may claim, and the
vision pass over the submitted photos. That pass no longer judges ownership. It
exists to catch harmful content (which earns a strike), to reject submissions
that plainly aren't a photo of a cat, and to record what vision saw so the
moderator can read it beside the cat's own record.

`utils.matching.score_candidate` is untouched and still drives sighting Re-ID.
It is simply no longer consulted about who owns a cat: exact matches on seven
categorical labels cannot tell two similar-looking cats apart, which is
tolerable when suggesting a match and not when granting someone a feed of where
an animal is being seen.
"""

from __future__ import annotations

import logging

from app.services.vision import CatFeatures, analyze_cat_photo

log = logging.getLogger(__name__)

# A claim takes all three photos, and each must show the claimant with the cat.
# Photos of a cat prove nothing on their own — anyone can take those — so the
# person in frame is what the moderator actually reads. /cats/register is a
# different case (no existing cat to take from) and still accepts one photo.
MIN_PHOTOS = 3
MAX_PHOTOS = 3
# After a rejection, the same user must wait this long before retrying the same cat.
CLAIM_COOLDOWN_HOURS = 24
# Max claim submissions per user per rolling day (DB-backed).
MAX_CLAIM_ATTEMPTS_PER_DAY = 5
# Claims awaiting a moderator, per user, across all cats. The daily cap alone
# stops nothing here: pending claims never resolve on their own, so without this
# one account could park an unbounded backlog in the queue.
MAX_OPEN_PENDING_CLAIMS = 3


async def analyze_claim_photos(photos_bytes: list[bytes]) -> list[CatFeatures]:
    """Run vision on each photo. Raises VisionError if the service is unavailable."""
    results: list[CatFeatures] = []
    for contents in photos_bytes:
        results.append(await analyze_cat_photo(contents))
    return results


def invalid_photo_reason(photo_features: list[CatFeatures]) -> str | None:
    """Why this claim can't be reviewed at all, or None if it can.

    Submission validity, not an ownership judgement. It keeps photos of dogs out
    of a queue a person has to read, and stops there: a second cat in frame is
    not a defect in a claim. Claims ask for the claimant at home with the cat,
    and a household with two cats produces exactly that photo — rejecting it
    would turn the most ordinary evidence there is into an error message, before
    any human saw it. The moderator is told the count instead
    (routers/moderation.py) and reads the photo themselves.
    """
    for i, f in enumerate(photo_features, start=1):
        if not f.is_cat:
            return f"Photo {i} doesn't appear to contain a cat."
    return None


def invalid_registration_photo_reason(photo_features: list[CatFeatures]) -> str | None:
    """The same, for /cats/register, which additionally needs one cat per photo.

    Registration photos aren't only evidence: approving one builds the new Cat
    out of the features vision read from them. With two cats in frame there is
    no saying which animal those features describe, so the record would be built
    from a guess. A claim has an existing cat to compare against and doesn't.
    """
    reason = invalid_photo_reason(photo_features)
    if reason is not None:
        return reason
    for i, f in enumerate(photo_features, start=1):
        if f.cat_count > 1:
            return f"Photo {i} contains more than one cat. Please photograph your cat alone."
    return None
