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
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings

# Image tokens ≈ (width × height) / 750, and the API auto-downscales anything
# over ~1.15 MP — so oversized uploads just pay the ~1,600-token ceiling.
# Feature analysis (coat colour/pattern/fur length from closed enums) works at
# ~1092px on the long edge (~1,100-1,200 tokens).
ANALYSIS_MAX_DIMENSION = 1092
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

# Harm categories that count as content strikes against the submitting account
# (see services/moderation.py). Deliberately excludes benign misses like "not a
# cat" or blurry photos — only content that shouldn't be pointed at a camera.
INAPPROPRIATE_REASONS = [
    "animal_harm",
    "violence_or_gore",
    "nsfw",
    "hate_or_harassment",
    "private_information",
    "other_inappropriate",
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
                "description": (
                    "True only if at least one domestic cat (Felis catus) is clearly "
                    "visible. A dog is NOT a cat — set false for dogs, and for foxes, "
                    "raccoons, or any other animal. When unsure whether the animal is a "
                    "cat or a dog, set false."
                ),
            },
            "is_appropriate": {
                "type": "boolean",
                "description": (
                    "False if the photo contains content unsuitable for a general-audience "
                    "community app: violence, gore, animal cruelty or distress, nudity or "
                    "sexual content, hate symbols, identifiable private documents, or shock "
                    "content. A photo that merely lacks a cat is still appropriate unless "
                    "it contains such content."
                ),
            },
            "inappropriate_reason": {
                "type": ["string", "null"],
                "enum": [*INAPPROPRIATE_REASONS, None],
                "description": "Single best category when is_appropriate is false; null otherwise.",
            },
            "cat_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of distinct cats clearly visible in the photo.",
            },
            "not_cat_reason": {
                "type": ["string", "null"],
                "description": (
                    "Short reason when is_cat is false — name the actual subject, e.g. "
                    "'dog', 'fox', 'blurry', 'no animal'. Null otherwise."
                ),
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
        "required": ["is_cat", "cat_count", "is_appropriate"],
    },
}

SYSTEM_PROMPT = (
    "You are a cat re-identification assistant. "
    "Analyze the photo and call the `report_cat` tool exactly once. "
    "Only a domestic cat (Felis catus) counts as a cat. A dog is NOT a cat: dogs, "
    "foxes, and other animals must set is_cat=false with not_cat_reason naming the "
    "animal (e.g. 'dog'). Dogs — especially small or fluffy breeds — can resemble "
    "cats, so look carefully at ears, muzzle and eyes; if it is a dog, or you are "
    "not confident the animal is a cat, set is_cat=false. "
    "If no cat is clearly visible set is_cat=false and leave feature fields null. "
    "Also screen the photo for harmful content: set is_appropriate=false with an "
    "inappropriate_reason for violence, gore, animal cruelty, nudity or sexual "
    "content, hate symbols, private documents, or shock content. Ordinary photos "
    "that merely lack a cat are still appropriate. "
    "Be conservative: when a feature is ambiguous, return null or 'unknown' rather than guessing."
)

USER_PROMPT = (
    "Identify the cat in this photo and report its visual features via the report_cat tool. "
    "Count every distinct cat you can see in cat_count."
)


@dataclass
class CatFeatures:
    is_cat: bool
    cat_count: int
    # Content screening: False means the photo contains harmful content and the
    # submitting account should receive a content strike (services/moderation.py).
    is_appropriate: bool = True
    inappropriate_reason: str | None = None
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


def _prepare_image_for_api(image_bytes: bytes, max_dimension: int) -> bytes:
    """Shrink and re-encode image as JPEG for the Anthropic call.

    The on-disk original is left untouched; only this in-memory copy is sent
    to the API. Longest side is capped at max_dimension; aspect ratio
    is preserved (no cropping — keeps the cat in frame regardless of how the
    user composed the shot).
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Respect EXIF orientation so the model sees the photo upright, the
            # same way the stored copy is normalised in utils/upload.py.
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            longest = max(img.width, img.height)
            if longest > max_dimension:
                scale = max_dimension / longest
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
    api_image = _prepare_image_for_api(image_bytes, ANALYSIS_MAX_DIMENSION)
    media_type = "image/jpeg"
    image_b64 = base64.standard_b64encode(api_image).decode("ascii")

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            temperature=0,
            # NOTE: these cache_control markers are currently a no-op — the
            # static prefix (system prompt + tool schema) is well under the
            # model's minimum cacheable prefix (4096 tokens on Haiku 4.5), so
            # nothing is written to the cache. Harmless to keep; they'd start
            # working if the prefix ever grows past the minimum. The per-call
            # image is never cacheable either way.
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
                    # Fail open if the model omits the flag — a schema hiccup
                    # must not hand out strikes.
                    is_appropriate=bool(payload.get("is_appropriate", True)),
                    inappropriate_reason=payload.get("inappropriate_reason"),
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

