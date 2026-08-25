#!/usr/bin/env python3
"""Fetch candidate backdrops from Pexels FOR REVIEW.

Nothing downloaded here is used by anything. Stock libraries are searched by
keyword, so what comes back cannot be known in advance, and an unattended
render putting an unseen clip behind worship lyrics is not a risk worth
carrying. Every clip arrives marked unapproved and stays invisible to the
renderer until a person has watched it and said otherwise.

Approve with scripts/approve_footage.py after looking at the contact sheet.

    python scripts/fetch_footage.py                    # every mood, 3 clips each
    python scripts/fetch_footage.py --mood warm -n 5
    python scripts/fetch_footage.py --credits          # print the credit list

Downloads raw clips, then grades and loops each one into something lyrics can
sit on top of. Both steps are idempotent, so re-running only fetches what is
actually missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import footage  # noqa: E402

#: Per-mood grading. `target_luma` is the mean brightness each clip is
#: normalised TO, not an amount to subtract — stock clips arrive anywhere from
#: near-black to blown out, so only a target survives contact with real
#: footage. ~75-85 suits white type: dark enough to read against, bright
#: enough that you can still see what the footage is.
#: 'bright' is the exception — Morning Light sets navy type on a light ground,
#: so its footage must stay bright or the dark text vanishes into it.
GRADE = {
    "warm":       dict(target_luma=78, saturation=0.82, blur=1.2, slow=1.35),
    "clean":      dict(target_luma=70, saturation=0.70, blur=2.0, slow=1.5),
    "reverent":   dict(target_luma=74, saturation=0.80, blur=1.6, slow=1.4),
    "reflective": dict(target_luma=82, saturation=0.86, blur=1.0, slow=1.5),
    "bright":     dict(target_luma=150, saturation=0.72, blur=1.8, slow=1.35),
    "hopeful":    dict(target_luma=80, saturation=0.84, blur=1.2, slow=1.3),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mood", help="only this mood")
    ap.add_argument("-n", "--per-mood", type=int, default=3,
                    help="clips to keep per mood (default 3)")
    ap.add_argument("--seconds", type=float, default=14.0,
                    help="trim each prepared clip to this length before looping")
    ap.add_argument("--credits", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-grade existing clips")
    args = ap.parse_args()

    catalog = footage.load_catalog()

    if args.credits:
        print(footage.credits(catalog))
        return 0

    moods = [args.mood] if args.mood else list(footage.MOODS)
    for mood in moods:
        have = [c for c in catalog.values() if c.mood == mood]
        need = args.per_mood - len(have)
        if need <= 0:
            print(f"[{mood}] already have {len(have)}")
            continue

        print(f"[{mood}] need {need} more")
        # Gather a surplus of candidates: exposure is only knowable after
        # download, and a good fraction of stock results turn out unusable.
        candidates = []
        for query in footage.MOODS[mood]:
            try:
                results = footage.search(query, mood=mood, per_page=12)
            except footage.PexelsError as exc:
                print(f"   search failed ({query}): {exc}")
                continue
            for clip in results:
                if clip.id in catalog or any(c.id == clip.id for c in candidates):
                    continue
                candidates.append(clip)

        grade = GRADE.get(mood, {})
        kept = 0
        for clip in candidates:
            if kept >= need:
                break
            try:
                print(f"   {clip.id} ({clip.duration}s, {clip.author})…",
                      end=" ", flush=True)
                footage.download(clip)
                footage.prepare(clip, seconds=args.seconds, force=args.force, **grade)
                catalog[clip.id] = clip
                footage.save_catalog(catalog)
                kept += 1
                print(f"kept -> {clip.prepared}")
            except footage.UnusableClip as exc:
                # Not an error — stock libraries are full of footage that is
                # simply too dark or too blown out to sit behind lyrics.
                clip.raw_path.unlink(missing_ok=True)
                print(f"rejected ({exc})")
            except Exception as exc:
                print(f"FAILED: {exc}")
        if kept < need:
            print(f"   only found {kept}/{need} usable for [{mood}] "
                  f"— widen MOODS[{mood!r}] queries if you want more")

    print()
    total = sum(1 for c in catalog.values() if c.prepared)
    print(f"library: {total} prepared clips across {len(set(c.mood for c in catalog.values()))} moods")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
