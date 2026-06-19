"""Claude Haiku vision recognition for cat photos.

Calls Anthropic's API with a strict tool_use schema so the model is forced to
return valid JSON. The schema doubles as the controlled vocabulary used for
Re-ID matching downstream — keep enums in sync with the columns on Sighting/Cat.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

import anthropic
from PIL import Image, UnidentifiedImageError

from app.config import settings

# Anthropic recommends ~1.15 MP (≈1092px) for image input. We sit slightly above
# that to keep a margin of detail useful for distinguishing_marks while staying
# below the threshold where they auto-downscale server-side.
MAX_IMAGE_DIMENSION = 1568
JPEG_QUALITY = 85

log = logging.getLogger(__name__)

PRIMARY_COLORS = ["black", "white", "orange", "gray", "brown", "cream", "mixed"]
SECONDARY_COLORS = ["black", "white", "orange", "gray", "brown", "cream"]
PATTERNS = ["solid", "tabby", "bicolor", "tricolor", "tortoiseshell", "calico", "pointed"]
FUR_LENGTHS = ["short", "medium", "long"]
EYE_COLORS = ["green", "yellow", "blue", "orange", "heterochromatic", "unknown"]
BODY_SIZES = ["small", "medium", "large"]

# Closed list of cat categories — drives the "what species is this" label shown
# to users. Order doesn't matter to the model; group is for human readability.
CAT_BREEDS = [
    # Tabby variants
    "Orange Tabby",
    "Brown Tabby",
    "Gray Tabby",
    "Silver Tabby",
    # Solid colours
    "Black Cat",
    "White Cat",
    "Gray Cat",
    "Cream Cat",
    # Bicolor patterns
    "Tuxedo Cat",        # black + white only
    "Gray and White",
    "Brown and White",
    "Orange and White",
    # Multi-colour patterns
    "Calico",            # white + orange + black
    "Tortoiseshell",     # black + orange, no/minimal white
    # Long-haired non-purebred
    "Long-haired Tabby",
    "Long-haired Tuxedo",
    # Recognisable purebreds (use ONLY when defining features are clearly visible)
    "Maine Coon",
    "Persian",
    "Siamese",
    "Ragdoll",
    "Bengal",
    "Russian Blue",
    "Sphinx",
    "Scottish Fold",
    "British Shorthair",
    "Abyssinian",
    "Birman",
    "Norwegian Forest Cat",
    # Last-resort fallback when nothing else fits
    "Mixed Cat",
]

REPORT_CAT_TOOL: dict[str, Any] = {
    "name": "report_cat",
    "description": (
        "Report structured visual features of the cat in the photo. "
        "Used downstream for re-identifying the same individual cat across sightings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_cat": {
                "type": "boolean",
                "description": "True if at least one cat is clearly visible.",
            },
            "cat_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of distinct cats clearly visible in the photo.",
            },
            "not_cat_reason": {
                "type": ["string", "null"],
                "description": "Short reason if is_cat is false (e.g. 'dog', 'blurry', 'no animal'). Null otherwise.",
            },
            "primary_color": {
                "type": ["string", "null"],
                "enum": [*PRIMARY_COLORS, None],
                "description": "Dominant coat color.",
            },
            "secondary_color": {
                "type": ["string", "null"],
                "enum": [*SECONDARY_COLORS, None],
                "description": "Second coat color if bicolor/tricolor/calico, otherwise null.",
            },
            "pattern": {
                "type": ["string", "null"],
                "enum": [*PATTERNS, None],
            },
            "fur_length": {
                "type": ["string", "null"],
                "enum": [*FUR_LENGTHS, None],
            },
            "eye_color": {
                "type": ["string", "null"],
                "enum": [*EYE_COLORS, None],
            },
            "body_size": {
                "type": ["string", "null"],
                "enum": [*BODY_SIZES, None],
                "description": "Estimated adult body size. 'small' = kitten/petite, 'large' = oversized adult.",
            },
            "breed": {
                "type": ["string", "null"],
                "enum": [*CAT_BREEDS, None],
                "description": (
                    "Pick the single most accurate cat category from the enum. "
                    "Selection priority: (1) if the cat clearly shows a purebred's "
                    "defining features (e.g. Sphinx hairless, Scottish Fold folded "
                    "ears, Bengal spotted/marbled, Siamese/Ragdoll/Birman pointed, "
                    "Persian flat face), choose that breed; (2) otherwise pick the "
                    "coat-type label that best describes the visible colour and "
                    "pattern (Orange Tabby for orange + tabby, Tuxedo Cat for "
                    "black + white only, Gray and White / Brown and White / Orange "
                    "and White for other bicolors, Calico for white + orange + "
                    "black tricolor, Tortoiseshell for black + orange with no "
                    "white, etc.); (3) only use 'Mixed Cat' as a last resort when "
                    "the cat genuinely fits no other category."
                ),
            },
        },
        "required": ["is_cat", "cat_count"],
    },
}

SYSTEM_PROMPT = (
    "You are a cat re-identification assistant. "
    "Analyze the photo and call the `report_cat` tool exactly once. "
    "If no cat is clearly visible set is_cat=false and leave feature fields null. "
    "Be conservative: when a feature is ambiguous, return null or 'unknown' rather than guessing."
)

USER_PROMPT = (
    "Identify the cat in this photo and report its visual features via the report_cat tool. "
    "Count every distinct cat you can see in cat_count."
)


REJECTION_REASONS = [
    "not_a_cat",
    "animal_harm",
    "violence_or_gore",
    "nsfw",
    "hate_or_harassment",
    "private_information",
    "other_inappropriate",
]

REVIEW_POST_TOOL: dict[str, Any] = {
    "name": "review_post",
    "description": "Moderate a photo submitted to a public cat-photo feed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_cat": {
                "type": "boolean",
                "description": "True if at least one real cat is clearly the subject of the photo.",
            },
            "is_appropriate": {
                "type": "boolean",
                "description": (
                    "False if the photo contains content unsuitable for a general-audience "
                    "community feed: violence, gore, animal cruelty or distress, nudity or "
                    "sexual content, hate symbols, identifiable private documents, or shock content."
                ),
            },
            "rejection_reason": {
                "type": ["string", "null"],
                "enum": [*REJECTION_REASONS, None],
                "description": "Single best reason when rejecting; null when the photo is acceptable.",
            },
            "reason_detail": {
                "type": ["string", "null"],
                "description": (
                    "One short, friendly sentence suitable to show the uploader explaining "
                    "the rejection. Null when the photo is acceptable."
                ),
            },
        },
        "required": ["is_cat", "is_appropriate"],
    },
}

MODERATION_SYSTEM = (
    "You are a content moderator for a friendly neighborhood cat-spotting app. "
    "Review the photo and call the `review_post` tool exactly once. "
    "Accept photos where a real cat is the clear subject. Reject photos with no cat "
    "(dogs, memes, screenshots, drawings, unrelated scenes) and photos containing harmful "
    "or inappropriate content of any kind. Be strict about harm, lenient about photo quality."
)

MODERATION_USER_PROMPT = (
    "Review this photo for the public cat feed and report your decision via the review_post tool."
)


@dataclass
class ModerationResult:
    is_cat: bool
    is_appropriate: bool
    rejection_reason: str | None = None
    reason_detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.is_cat and self.is_appropriate


@dataclass
class CatFeatures:
    is_cat: bool
    cat_count: int
    not_cat_reason: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    fur_length: str | None = None
    eye_color: str | None = None
    body_size: str | None = None
    breed: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class VisionError(RuntimeError):
    pass


NICKNAME_SYSTEM = (
    "You are a cat-naming assistant. Given a brief description of a cat's appearance, "
    "respond with a single short, cute, memorable name — one or two words maximum. "
    "No explanation, punctuation, or quotes — just the name itself."
)


def generate_cat_nickname(
    breed: str | None,
    primary_color: str | None,
    secondary_color: str | None,
    pattern: str | None,
    body_size: str | None,
    vibes: str | None,
) -> str | None:
    """Return a cute nickname for a newly discovered cat, or None on any failure."""
    if not settings.anthropic_api_key:
        return None
    try:
        parts = [p for p in [breed, primary_color, secondary_color, pattern, body_size] if p]
        if vibes:
            parts.append(f"vibes: {vibes}")
        description = ", ".join(parts) or "unknown cat"

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=20,
            system=NICKNAME_SYSTEM,
            messages=[{"role": "user", "content": f"Cat: {description}"}],
        )
        name = response.content[0].text.strip().strip("\"'")
        if name and len(name) <= 40:
            return name
    except Exception:
        log.warning("Nickname generation failed", exc_info=True)
    return None


def _prepare_image_for_api(image_bytes: bytes) -> bytes:
    """Shrink and re-encode image as JPEG for the Anthropic call.

    The on-disk original is left untouched; only this in-memory copy is sent
    to the API. Longest side is capped at MAX_IMAGE_DIMENSION; aspect ratio
    is preserved (no cropping — keeps the cat in frame regardless of how the
    user composed the shot).
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            longest = max(img.width, img.height)
            if longest > MAX_IMAGE_DIMENSION:
                scale = MAX_IMAGE_DIMENSION / longest
                new_size = (round(img.width * scale), round(img.height * scale))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise VisionError(f"Unable to decode image: {exc}") from exc


async def analyze_cat_photo(image_bytes: bytes) -> CatFeatures:
    """Send image to Claude Haiku and return structured cat features.

    Raises VisionError on API failure or malformed tool output.
    """
    if not settings.anthropic_api_key:
        raise VisionError("ANTHROPIC_API_KEY is not configured")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    api_image = _prepare_image_for_api(image_bytes)
    media_type = "image/jpeg"
    image_b64 = base64.standard_b64encode(api_image).decode("ascii")

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            temperature=0,
            # Cache the static prefix (system prompt + tool schema). Subsequent
            # calls within the 5-minute TTL pay ~10% of the input price for the
            # cached portion. Anthropic charges only the per-call image and
            # output tokens at full rate — saves ~25% per photo at MVP scale.
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[{**REPORT_CAT_TOOL, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "report_cat"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": USER_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        log.exception("Anthropic API call failed")
        raise VisionError(f"Anthropic API error: {exc}") from exc

    for block in response.content:
        if block.type == "tool_use" and block.name == "report_cat":
            payload = block.input
            try:
                return CatFeatures(
                    is_cat=bool(payload["is_cat"]),
                    cat_count=int(payload["cat_count"]),
                    not_cat_reason=payload.get("not_cat_reason"),
                    primary_color=payload.get("primary_color"),
                    secondary_color=payload.get("secondary_color"),
                    pattern=payload.get("pattern"),
                    fur_length=payload.get("fur_length"),
                    eye_color=payload.get("eye_color"),
                    body_size=payload.get("body_size"),
                    breed=payload.get("breed"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise VisionError(f"Malformed tool_use payload: {payload!r}") from exc

    raise VisionError("Model did not return a report_cat tool_use block")


async def moderate_explorer_photo(image_bytes: bytes) -> ModerationResult:
    """Moderate a direct Explorer upload: must contain a cat and no harmful content.

    Raises VisionError on API failure or malformed tool output — callers should
    fail closed (reject the upload) in that case.
    """
    if not settings.anthropic_api_key:
        raise VisionError("ANTHROPIC_API_KEY is not configured")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    api_image = _prepare_image_for_api(image_bytes)
    image_b64 = base64.standard_b64encode(api_image).decode("ascii")

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=256,
            temperature=0,
            # Static prefix (system + tool) is byte-stable so prompt caching applies,
            # same as analyze_cat_photo.
            system=[{
                "type": "text",
                "text": MODERATION_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[{**REVIEW_POST_TOOL, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "review_post"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": MODERATION_USER_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        log.exception("Anthropic moderation call failed")
        raise VisionError(f"Anthropic API error: {exc}") from exc

    for block in response.content:
        if block.type == "tool_use" and block.name == "review_post":
            payload = block.input
            try:
                return ModerationResult(
                    is_cat=bool(payload["is_cat"]),
                    is_appropriate=bool(payload["is_appropriate"]),
                    rejection_reason=payload.get("rejection_reason"),
                    reason_detail=payload.get("reason_detail"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise VisionError(f"Malformed tool_use payload: {payload!r}") from exc

    raise VisionError("Model did not return a review_post tool_use block")
