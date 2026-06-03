"""Generate the three PWA icons from a source logo PNG/ICO.

Outputs (overwritten on each run):
  static/icon-192.png            192x192, plain logo,        purpose:"any"
  static/icon-512.png            512x512, plain logo,        purpose:"any"
  static/icon-maskable-512.png   512x512, COVER-scaled logo
                                 on dark #13121a bg,         purpose:"maskable"

Re-run after replacing the source logo, bump ICON_VER in app.py, then
remove + re-add the phone home-screen entry - the launcher caches icons by URL.
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

CANDIDATES = [
    APP_DIR / "icon.png",
    APP_DIR / "icon.ico",
]
BG = (19, 18, 26, 255)  # #13121a - app dark background
WHITE_THRESHOLD = 230   # min(R,G,B) > this -> treat as background, drop alpha


def find_source() -> Path:
    for c in CANDIDATES:
        if c.is_file():
            return c
    raise SystemExit(
        "No source icon found. Tried:\n  " +
        "\n  ".join(str(c) for c in CANDIDATES)
    )


def whitebg_to_transparent(src: Image.Image, thresh: int = WHITE_THRESHOLD) -> Image.Image:
    """Make near-white pixels transparent.

    Source icons designed with an opaque white card/folder treatment look
    wrong on a dark launcher tile - the white dominates. This drops the
    alpha to 0 wherever R, G, B are all above `thresh`. Anti-aliased
    edges and colored detail are preserved.
    """
    src = src.convert("RGBA")
    r, g, b, a = src.split()
    # Pixel-wise min(R, G, B)
    min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
    # 255 where pixel should KEEP its alpha, 0 where it should drop to transparent
    keep_mask = min_rgb.point(lambda v: 0 if v > thresh else 255)
    new_a = ImageChops.multiply(a, keep_mask)
    return Image.merge("RGBA", (r, g, b, new_a))


def make_plain(src: Image.Image, size: int) -> Image.Image:
    """Plain logo on transparent background, scaled to fit (contain)."""
    img = whitebg_to_transparent(src)
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    return canvas


def make_maskable(src: Image.Image, size: int) -> Image.Image:
    """COVER-scale logo content to fill the whole tile on the app's dark bg.

    1) Make the white card/background transparent so dark shows through.
    2) Tight-crop to the surviving colored content so the logo actually
       fills the tile - otherwise the launcher rounds corners off a tile
       that's mostly dark padding.
    3) COVER-scale and alpha-composite onto the dark background."""
    logo = whitebg_to_transparent(src)
    bbox = logo.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    cover = ImageOps.fit(logo, (size, size), Image.LANCZOS, centering=(0.5, 0.5))
    bg = Image.new("RGBA", (size, size), BG)
    bg.alpha_composite(cover)
    return bg


def main():
    src_path = find_source()
    print(f"Source: {src_path}")
    src = Image.open(src_path).convert("RGBA")

    out_192 = STATIC_DIR / "icon-192.png"
    out_512 = STATIC_DIR / "icon-512.png"
    out_mask = STATIC_DIR / "icon-maskable-512.png"

    make_plain(src, 192).save(out_192, "PNG")
    print(f"Wrote {out_192}")
    make_plain(src, 512).save(out_512, "PNG")
    print(f"Wrote {out_512}")
    make_maskable(src, 512).save(out_mask, "PNG")
    print(f"Wrote {out_mask}")


if __name__ == "__main__":
    main()
