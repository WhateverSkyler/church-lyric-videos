"""Regression tests for the parts that have already been wrong once.

Every test here corresponds to a bug that actually shipped into a render and
had to be found by measuring output rather than reading code. They are cheap
and they run without a GPU, footage library or network.

    python -m pytest tests/ -q          (or: python tests/test_engine.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from engine import ocr  # noqa: E402
from engine.align import Warp, chroma, dtw  # noqa: E402
from engine.brand import Fonts, Palette, hex_to_rgb, load_font, verify_assets  # noqa: E402
from engine.lyrics import LyricLine, LyricTrack  # noqa: E402
from engine.textanim import PRESETS  # noqa: E402
from engine.textcard import wrap_lines  # noqa: E402
from engine.themes import THEMES  # noqa: E402


# --------------------------------------------------------------------------
# brand
# --------------------------------------------------------------------------


def test_assets_present():
    assert verify_assets() == [], "brand assets are missing"


def test_variable_font_weights_actually_differ():
    """Merriweather is a variable font; the axis name is 'Weight', not 'wght'.

    Matching on the OpenType tag silently produced identical glyphs at every
    weight, so every theme rendered at Light.
    """
    widths = {
        name: load_font(getattr(Fonts, name), 60).getbbox("Hopewell")[2]
        for name in ("SERIF_LIGHT", "SERIF_REGULAR", "SERIF_BOLD", "SERIF_BLACK")
    }
    assert len(set(widths.values())) == 4, f"weights collapsed: {widths}"
    assert (widths["SERIF_LIGHT"] < widths["SERIF_REGULAR"]
            < widths["SERIF_BOLD"] < widths["SERIF_BLACK"])


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


def test_fingerprint_distance_is_scale_invariant():
    """Mean-absolute-difference silently merged an entire song into 11 blobs.

    Text is ~4% of a frame, so the ~96% of cells empty in both fingerprints
    dragged a plain mean to ~0 — below any workable threshold. The normalised
    metric must put clearly different frames near 1 regardless of ink volume.
    """
    a = np.zeros((54, 96), dtype=np.float32)
    b = np.zeros((54, 96), dtype=np.float32)
    a[20:24, 10:40] = 1.0
    b[20:24, 55:85] = 1.0

    assert ocr.distance(a.ravel(), a.ravel()) == pytest.approx(0.0, abs=1e-6)
    assert ocr.distance(a.ravel(), b.ravel()) > 0.9, "disjoint text must read as different"

    # Same shapes, ten times the ink: the distance must not move.
    assert ocr.distance((a * 10).ravel(), (b * 10).ravel()) == pytest.approx(
        ocr.distance(a.ravel(), b.ravel()), abs=1e-6)


def test_fingerprint_grid_resolves_separate_words():
    """A 12x48 grid put 720p text inside one row, so different lines matched."""
    left = np.zeros((720, 1280), dtype=bool)
    right = np.zeros((720, 1280), dtype=bool)
    left[350:400, 100:300] = True
    right[350:400, 900:1100] = True
    d = ocr.distance(ocr.fingerprint(left), ocr.fingerprint(right))
    assert d > 0.9, f"grid too coarse to separate positions (distance {d})"


def test_text_mask_rejects_bright_saturated_background():
    """White type over a bright warm sky is the case that breaks brightness-only."""
    frame = np.zeros((40, 40, 3), dtype=np.uint8)
    frame[:, :] = (250, 180, 40)     # bright but strongly saturated: sky
    frame[10:20, 10:20] = (252, 252, 252)  # bright and neutral: the type
    mask = ocr.text_mask(frame)
    assert mask[12, 12], "white type should be kept"
    assert not mask[2, 2], "saturated sky should be rejected"


def test_shouted_lines_are_normalised_but_mixed_case_is_not():
    assert ocr.normalise_case("ALL MY DAYS") == "All my days"
    assert ocr.normalise_case("Already Mixed Case") == "Already Mixed Case"
    # Reverent capitalisation survives.
    assert "God" in ocr.normalise_case("SING OF THE GOODNESS OF GOD")
    assert ocr.normalise_case("WHERE I GO") .split()[1] == "I"
    # Row structure is preserved.
    assert ocr.normalise_case("ONE TWO\nTHREE FOUR").count("\n") == 1


def test_clean_repairs_bar_for_capital_i():
    assert ocr.clean("| WILL SING") == "I will sing"


# --------------------------------------------------------------------------
# typography
# --------------------------------------------------------------------------


def test_wrapping_leaves_no_orphan_word():
    draw = ImageDraw.Draw(Image.new("L", (8, 8)))
    font = load_font(Fonts.SERIF_BOLD, 82)
    max_px = 1920 * 0.72
    text = "Living life together in this place and every voice"
    rows = wrap_lines(text, font, draw, max_px, 1.6)
    assert len(rows) >= 2
    widths = [draw.textlength(r, font=font) for r in rows]
    assert widths[-1] >= widths[-2] * 0.40, f"orphan row: {rows}"
    # Nothing may be lost or reordered by balancing.
    assert " ".join(rows).split() == text.split()


def test_wrapping_respects_explicit_breaks():
    draw = ImageDraw.Draw(Image.new("L", (8, 8)))
    font = load_font(Fonts.SERIF_BOLD, 60)
    rows = wrap_lines("first row\nsecond row", font, draw, 4000, 0)
    assert rows == ["first row", "second row"]


# --------------------------------------------------------------------------
# lyrics model
# --------------------------------------------------------------------------


def test_lyr_round_trip_preserves_breaks_and_timings(tmp_path):
    track = LyricTrack(title="Test")
    track.lines.append(LyricLine("one line", 1.0, 3.5))
    track.lines.append(LyricLine("split\nacross rows", 4.0, 8.25))
    path = tmp_path / "t.lyr"
    track.to_lyr(path)
    back = LyricTrack.load(path)
    assert back.title == "Test"
    assert [l.text for l in back.lines] == ["one line", "split\nacross rows"]
    assert back.lines[1].start == pytest.approx(4.0, abs=0.01)
    assert back.lines[1].end == pytest.approx(8.25, abs=0.01)


def test_tidy_merges_repeats_and_removes_overlap():
    track = LyricTrack()
    track.lines = [
        LyricLine("same words", 1.0, 3.0),
        LyricLine("same words", 3.4, 5.0),   # OCR saw it again a moment later
        LyricLine("next", 4.5, 7.0),          # overlaps the one before it
    ]
    tidied = track.tidy()
    assert len(tidied) == 2, "consecutive duplicates should merge"
    assert tidied.lines[0].end <= tidied.lines[1].start


def test_clamp_trims_past_the_end_of_audio():
    track = LyricTrack()
    track.lines = [LyricLine("a", 1.0, 5.0), LyricLine("b", 9.0, 12.0)]
    out = track.clamp(10.0)
    assert len(out) == 2
    assert out.lines[-1].end == pytest.approx(10.0)


# --------------------------------------------------------------------------
# animation
# --------------------------------------------------------------------------


def test_stagger_cannot_outrun_the_line():
    """A long line must have every word visible before the line starts leaving."""
    anim = PRESETS["lift"]
    count, duration = 12, 2.6
    lead = anim.total_lead_in(count, duration)
    assert lead <= duration, f"last word arrives at {lead:.2f}s of a {duration}s line"
    tf = anim.transform_for(count - 1, count, duration * 0.75, duration)
    assert tf.opacity > 0.05, "final word never becomes visible"


def test_a_line_arrives_as_one_unit():
    """No preset may stagger words. A reader cannot take in a line until its
    last word lands, so staggering delays comprehension of every line — and it
    stretches the entrance far enough that cues land late on cards that butt
    together (measured at 433 ms worst case before this was removed)."""
    for key, anim in PRESETS.items():
        assert anim.stagger == 0.0, f"{key} staggers words"
        first = anim.transform_for(0, 6, 0.25, 5.0)
        last = anim.transform_for(5, 6, 0.25, 5.0)
        assert first.opacity == pytest.approx(last.opacity, abs=1e-6), key


def test_type_is_legible_well_before_its_entrance_finishes():
    """Opacity must outrun the motion, or a line reads as arriving late."""
    for key, anim in PRESETS.items():
        readable = anim.readable_lead_in(5, 6.0)
        settled = anim.total_lead_in(5, 6.0)
        assert readable < settled, f"{key} is only legible once it stops moving"
        # At the point we call it readable, it must actually be mostly opaque.
        tf = anim.transform_for(0, 5, readable, 6.0)
        assert tf.opacity > 0.55, f"{key} only {tf.opacity:.2f} opaque at its cue"


def test_every_theme_has_a_usable_animation_and_mood():
    for key, theme in THEMES.items():
        assert theme.mood, f"{key} has no mood, so it cannot pick footage"
        assert theme.animation is not None
        tf = theme.animation.transform_for(0, 4, 1.0, 4.0)
        assert 0.0 <= tf.opacity <= 1.0


# --------------------------------------------------------------------------
# alignment
# --------------------------------------------------------------------------


def test_dtw_recovers_a_known_tempo_change():
    """The Phase 2 guarantee: timings transfer between differing tempos."""
    rng = np.random.default_rng(0)
    base = rng.random((300, 12)).astype(np.float32)
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    # Stretch by 1.25 via nearest-neighbour resampling.
    idx = np.clip((np.arange(375) / 1.25).astype(int), 0, len(base) - 1)
    stretched = base[idx]

    warp = dtw(base, stretched)
    from engine.align import FEATURE_FPS

    for frame in (60, 120, 200):
        t = frame / FEATURE_FPS
        assert warp(t) == pytest.approx(t * 1.25, abs=0.35)
    assert warp.confidence() > 0.4


def test_warp_confidence_is_low_for_nonsense():
    warp = Warp(np.array([0.0, 1.0, 2.0]), np.array([0.0, 9.0, 0.5]))
    assert warp.confidence() < 0.6


def test_chroma_is_unit_normalised():
    sr = 16000
    t = np.linspace(0, 2, sr * 2, dtype=np.float32)
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    feats = chroma(tone, sr)
    norms = np.linalg.norm(feats, axis=1)
    assert np.allclose(norms[norms > 0], 1.0, atol=1e-3)


# --------------------------------------------------------------------------
# themes
# --------------------------------------------------------------------------


def test_theme_keys_and_palette_are_sane():
    assert len(THEMES) >= 6
    for key, theme in THEMES.items():
        assert theme.key == key
        assert theme.description
        assert 0.0 < theme.logo.opacity <= 1.0
        assert theme.logo.anchor[0] in "tb" and theme.logo.anchor[1] in "lcr"


def test_palette_entries_parse_as_colours():
    for name in dir(Palette):
        if name.isupper():
            value = getattr(Palette, name)
            if isinstance(value, str) and value.startswith("#"):
                assert len(hex_to_rgb(value)) == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
