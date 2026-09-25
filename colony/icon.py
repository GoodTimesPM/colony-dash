"""The colony's mark: a longhouse with the fire lit.

Drawn in code so it differs from every other Python app's icon and stays
editable. Built for 16px first: one silhouette, one warm accent, detail only
at larger sizes. Frames are drawn at 4x and downsampled, since Pillow's
polygon fill has no antialiasing.
"""

from __future__ import annotations

from pathlib import Path

ICON_PATH = Path(__file__).resolve().parent / "ui" / "colony.ico"
PNG_PATH = ICON_PATH.with_suffix(".png")

# Every size Windows asks for, so the shell never scales the 256 down.
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

NIGHT = (18, 14, 11, 255)      # the ground the settlement sits on
EARTH = (46, 38, 32, 255)      # the hill
THATCH = (232, 165, 74, 255)   # the roof, lit from the side
WALL = (196, 128, 58, 255)     # daub, one step darker so the roof reads first
FIRE = (255, 214, 130, 255)    # the doorway
SMOKE = (120, 104, 92, 255)


def _draw(px: int):
    """One frame at `px` pixels, drawn 4× and resampled down."""
    from PIL import Image, ImageDraw

    S = px * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1, fill):
        d.rectangle([x0 * S, y0 * S, x1 * S, y1 * S], fill=fill)

    def poly(points, fill):
        d.polygon([(x * S, y * S) for x, y in points], fill=fill)

    # The tile. Rounded, so it reads as an app icon and not a screenshot.
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=S * 0.22, fill=NIGHT)

    # The hill. A flat baseline would leave the house floating in a box.
    d.ellipse([-S * 0.35, S * 0.74, S * 1.35, S * 1.5], fill=EARTH)

    detail = px >= 40

    if detail:
        # A second, smaller house behind.
        poly([(0.79, 0.33), (0.99, 0.53), (0.59, 0.53)], THATCH)
        box(0.67, 0.51, 0.94, 0.79, WALL)

    # Overhanging eaves make the triangle read as a roof.
    poly([(0.40, 0.14), (0.78, 0.52), (0.02, 0.52)], THATCH)
    box(0.11, 0.50, 0.69, 0.82, WALL)

    # The fire: the one bright pixel at 16px.
    box(0.32, 0.60, 0.48, 0.82, FIRE)

    if detail:
        # Smoke, leaning with the wind off the ridge.
        for i, (x, y, r) in enumerate([(0.40, 0.10, 0.020), (0.44, 0.06, 0.016),
                                       (0.49, 0.028, 0.012)]):
            d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S],
                      fill=SMOKE[:3] + (150 - i * 35,))

    return img.resize((px, px), Image.LANCZOS)


def build(path: Path = ICON_PATH) -> Path:
    """Write the multi-resolution .ico (and a .png beside it for anything else)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = [_draw(s) for s in SIZES]
    big = frames[-1]
    big.save(PNG_PATH)
    # Carry the hand-drawn small sizes into the .ico instead of rescaled ones.
    big.save(path, format="ICO", sizes=[(s, s) for s in SIZES],
             append_images=frames[:-1])
    return path


def ensure(path: Path = ICON_PATH) -> Path | None:
    """The icon, built on demand, or None. A missing Pillow must not stop the
    window opening.
    """
    try:
        if path.is_file():
            return path
        return build(path)
    except Exception:
        return None


if __name__ == "__main__":
    print(build())
