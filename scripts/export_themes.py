#!/usr/bin/env python3
"""Write dashboard/themes.json from the live theme pack.

The dashboard runs on a small VPS and only ever needs each theme's name,
mood and description to draw the picker. Importing engine.themes there would
drag numpy and Pillow onto a box with a few hundred MB of headroom, purely to
read six strings. Run this whenever a theme is added or renamed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.themes import listing  # noqa: E402


def main() -> int:
    out = ROOT / "dashboard" / "themes.json"
    out.write_text(json.dumps(listing(), indent=2) + "\n", encoding="utf-8")
    print(f"{len(listing())} themes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
