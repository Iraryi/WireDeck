"""Generate crisp hardware-inspired RJ45 PNG and multi-size Windows .ico for WireDeck."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def render_logo_rgba(size: int = 1024) -> Image.Image:
    """Render clean flat RJ45 Ethernet port icon via supersampled Pillow drawing."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size / 256.0

    # Solid charcoal hardware tile (zero neon gradient)
    draw.rounded_rectangle(
        [16 * s, 16 * s, 240 * s, 240 * s],
        radius=40 * s,
        fill=(36, 41, 47, 255),
        outline=(59, 67, 77, 255),
        width=max(2, int(round(4 * s))),
    )

    # RJ45 stepped jack cavity
    cavity = [
        (56 * s, 64 * s),
        (200 * s, 64 * s),
        (200 * s, 168 * s),
        (164 * s, 168 * s),
        (164 * s, 192 * s),
        (92 * s, 192 * s),
        (92 * s, 168 * s),
        (56 * s, 168 * s),
    ]
    draw.polygon(cavity, fill=(22, 27, 34, 255))
    stroke_w = max(2, int(round(10 * s)))
    for i in range(len(cavity)):
        p1 = cavity[i]
        p2 = cavity[(i + 1) % len(cavity)]
        draw.line([p1, p2], fill=(208, 215, 222, 255), width=stroke_w)
        r = stroke_w / 2.0
        draw.ellipse([p1[0] - r, p1[1] - r, p1[0] + r, p1[1] + r], fill=(208, 215, 222, 255))

    # 4 crisp gold contact pins
    pin_color = (227, 179, 65, 255)
    for x in (82, 108, 136, 162):
        draw.rounded_rectangle(
            [x * s, 70 * s, (x + 12) * s, 114 * s],
            radius=3 * s,
            fill=pin_color,
        )

    # Authentic RJ45 link green LED (left) & activity amber LED (right)
    draw.rounded_rectangle([52 * s, 182 * s, 76 * s, 196 * s], radius=4 * s, fill=(45, 164, 78, 255))
    draw.rounded_rectangle([180 * s, 182 * s, 204 * s, 196 * s], radius=4 * s, fill=(210, 153, 34, 255))

    return img


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assets_dir = root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    master = render_logo_rgba(1024)
    png_out = assets_dir / "logo.png"
    master.resize((512, 512), Image.Resampling.LANCZOS).save(png_out, format="PNG")

    ico_out = assets_dir / "wiredeck.ico"
    icons = [master.resize(sz, Image.Resampling.LANCZOS) for sz in ICO_SIZES]
    icons[-1].save(ico_out, format="ICO", sizes=ICO_SIZES)
    print(f"Generated {png_out} and {ico_out} ({len(ICO_SIZES)} sizes)")


if __name__ == "__main__":
    main()
