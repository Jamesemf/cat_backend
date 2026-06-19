"""Smoke test for the vision pipeline.

Usage:
    python api/scripts/test_vision.py path/to/image1.jpg [path/to/image2.jpg ...]
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.vision import analyze_cat_photo  # noqa: E402


async def run_one(image_path: Path) -> None:
    print(f"\n=== {image_path.name} ({image_path.stat().st_size // 1024} KB) ===")
    features = await analyze_cat_photo(image_path.read_bytes())
    for k, v in asdict(features).items():
        print(f"  {k:24s} {v}")


async def main(paths: list[str]) -> None:
    for p in paths:
        path = Path(p)
        if not path.is_file():
            print(f"skip (not a file): {p}")
            continue
        try:
            await run_one(path)
        except Exception as exc:
            print(f"  ERROR: {exc!r}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1:]))
