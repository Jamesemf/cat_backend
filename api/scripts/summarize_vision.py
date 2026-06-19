"""Run vision recognition over many images and print a distribution summary.

Usage:
    python api/scripts/summarize_vision.py path/to/dir
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.vision import analyze_cat_photo, VisionError  # noqa: E402


async def analyze_one(path: Path) -> dict | None:
    try:
        features = await analyze_cat_photo(path.read_bytes())
        return {"file": path.name, **asdict(features)}
    except VisionError as exc:
        return {"file": path.name, "error": str(exc)}
    except Exception as exc:  # pragma: no cover — safety net for the loop
        return {"file": path.name, "error": f"{type(exc).__name__}: {exc}"}


async def main(image_dir: str) -> None:
    paths = sorted(Path(image_dir).glob("*.jpg"))
    print(f"Running vision on {len(paths)} images...")
    t0 = time.monotonic()

    # Sequential with a 5s gap — Anthropic Tier 1 caps Sonnet 4.6 at
    # 30k input tokens/min, and our 1568px JPEGs run ~2500 tokens each.
    results: list[dict | None] = []
    for p in paths:
        res = await analyze_one(p)
        results.append(res)
        sys.stdout.write(".")
        sys.stdout.flush()
        await asyncio.sleep(5)
    print(f"\nDone in {time.monotonic() - t0:.1f}s")

    errors = [r for r in results if r and r.get("error")]
    ok = [r for r in results if r and "error" not in r]
    not_cat = [r for r in ok if r["is_cat"] is False]
    multi_cat = [r for r in ok if (r["cat_count"] or 0) > 1]
    single_cat = [r for r in ok if r["is_cat"] and (r["cat_count"] or 0) == 1]

    print(f"\n=== TOTALS ===")
    print(f"  succeeded:     {len(ok)}/{len(paths)}")
    print(f"  single cat:    {len(single_cat)}")
    print(f"  no cat:        {len(not_cat)}")
    print(f"  multi cat:     {len(multi_cat)}")
    print(f"  errors:        {len(errors)}")

    def show(label: str, key: str) -> None:
        c = Counter(r.get(key) for r in single_cat)
        print(f"\n--- {label} ---")
        for v, n in c.most_common():
            print(f"  {n:3d}  {v}")

    show("breed", "breed")
    show("primary_color", "primary_color")
    show("pattern", "pattern")
    show("fur_length", "fur_length")
    show("eye_color", "eye_color")
    show("body_size", "body_size")

    if not_cat:
        print(f"\n--- no-cat ({len(not_cat)}) ---")
        for r in not_cat:
            print(f"  {r['file']}: {r.get('not_cat_reason')}")
    if multi_cat:
        print(f"\n--- multi-cat ({len(multi_cat)}) ---")
        for r in multi_cat:
            print(f"  {r['file']}: cat_count={r['cat_count']}")
    if errors:
        print(f"\n--- errors ---")
        for r in errors:
            print(f"  {r['file']}: {r['error']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
