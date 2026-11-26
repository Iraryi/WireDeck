"""Generate high-DPI PNG and multi-resolution Windows .ico for WireDeck."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def render_logo_rgba(size: int = 1024) -> Image.Image:
    """Render the symbolic WireDeck mark cleanly via supersampled Pillow drawing."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size / 256.0

    # Background squircle gradient approximation
    bg_mask = Image.new("L", (size, size), 0)
    m_draw = ImageDraw.Draw(bg_mask)
    m_draw.rounded_rectangle(
        [12 * s, 12 * s, 244 * s, 244 * s],
        radius=54 * s,
        fill=255,
    )

    grad = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    g_px = grad.load()
    c1 = (11, 19, 36)
    c2 = (19, 34, 56)
    for y in range(size):
        ty = y / max(1, size - 1)
        r = int(c1[0] + (c2[0] - c1[0]) * ty)
        g = int(c1[1] + (c2[1] - c1[1]) * ty)
        b = int(c1[2] + (c2[2] - c1[2]) * ty)
        for x in range(size):
            g_px[x, y] = (r, g, b, 255)

    img.paste(grad, (0, 0), bg_mask)

    # Outer border
    draw.rounded_rectangle(
        [14 * s, 14 * s, 242 * s, 242 * s],
        radius=52 * s,
        outline=(30, 58, 95, 255),
        width=max(2, int(round(6 * s))),
    )

    white = (226, 232, 240, 255)
    emerald = (6, 214, 160, 255)
    cyan = (56, 189, 248, 255)
    slate = (148, 163, 184, 255)
    sw = max(2, int(round(7 * s)))

    # Subtle bottom arc accent
    draw.arc(
        [52 * s, 140 * s, 204 * s, 214 * s],
        start=20,
        end=160,
        fill=(16, 185, 129, 115),
        width=max(2, int(round(5 * s))),
    )

    # Left node: Laptop screen + base
    draw.rounded_rectangle(
        [38 * s, 88 * s, 90 * s, 126 * s],
        radius=7 * s,
        outline=white,
        width=sw,
    )
    draw.line([30 * s, 138 * s, 98 * s, 138 * s], fill=white, width=max(2, int(round(8 * s))))
    draw.ellipse([58 * s, 101 * s, 70 * s, 113 * s], fill=emerald)

    # Center: Gigabit cable segments + lightning bolt
    draw.line([92 * s, 107 * s, 116 * s, 107 * s], fill=emerald, width=sw)
    draw.line([142 * s, 107 * s, 166 * s, 107 * s], fill=cyan, width=sw)
    bolt = [
        (134 * s, 68 * s),
        (112 * s, 114 * s),
        (128 * s, 114 * s),
        (120 * s, 152 * s),
        (148 * s, 102 * s),
        (131 * s, 102 * s),
    ]
    draw.polygon(bolt, fill=emerald)

    # Right node: Console monolith + cyan LED
    draw.rounded_rectangle(
        [168 * s, 72 * s, 216 * s, 144 * s],
        radius=10 * s,
        outline=white,
        width=sw,
    )
    draw.ellipse([185 * s, 87 * s, 199 * s, 101 * s], fill=cyan)
    draw.line([181 * s, 126 * s, 203 * s, 126 * s], fill=slate, width=max(2, int(round(6 * s))))

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
