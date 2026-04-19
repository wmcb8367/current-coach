"""
Generate the Current Coach app icon.

Design: slate-950 navy background with 3 concentric cyan radar arcs (M2X
logo motif), a filled cyan center dot, and a bold white arrow with a
cyan glow slicing diagonally through the radar — the arrow represents
the current flowing from the measurement point.

Renders at 4x supersampling and downsamples to 1024x1024 for sharp edges.
Output goes straight into the app icon asset set.
"""
import math
from PIL import Image, ImageDraw, ImageFilter, ImageChops

# Colors
BG_NAVY = (2, 6, 23)          # slate-950 #020617
CYAN_400 = (34, 211, 238)     # #22d3ee — arcs + glow
WHITE = (255, 255, 255)

# Output & supersampling
SIZE = 1024
SSAA = 4
W = SIZE * SSAA
CX = CY = W // 2


def draw_arc_circle(img, cx, cy, radius, width, color, alpha):
    """Stroke a full-circle arc with a given alpha."""
    stroke = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(stroke)
    d.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=(*color, alpha),
        width=width,
    )
    img.alpha_composite(stroke)


def arrow_polygon(tail, tip, shaft_w, head_w, head_len):
    """Return the arrow polygon as a list of (x, y) tuples."""
    tx, ty = tail
    hx, hy = tip
    dx, dy = hx - tx, hy - ty
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length        # unit vector along arrow
    nx, ny = -uy, ux                         # left-normal unit vector

    # Base of the arrowhead — where the shaft ends and the head begins
    bx = hx - ux * head_len
    by = hy - uy * head_len

    shaft_half = shaft_w / 2
    head_half = head_w / 2

    return [
        (tx + nx * shaft_half, ty + ny * shaft_half),           # tail left
        (bx + nx * shaft_half, by + ny * shaft_half),           # base left (shaft)
        (bx + nx * head_half,  by + ny * head_half),            # base left (head flare)
        (hx, hy),                                                # tip
        (bx - nx * head_half,  by - ny * head_half),            # base right (head flare)
        (bx - nx * shaft_half, by - ny * shaft_half),           # base right (shaft)
        (tx - nx * shaft_half, ty - ny * shaft_half),           # tail right
    ]


def round_polygon(points, radii, segments=14):
    """Round each polygon corner by its given radius using a quadratic bezier
    (control point = original vertex) approximated as a polyline."""
    n = len(points)
    out = []
    for i in range(n):
        p_prev = points[(i - 1) % n]
        p_curr = points[i]
        p_next = points[(i + 1) % n]
        r = radii[i]

        v1 = (p_prev[0] - p_curr[0], p_prev[1] - p_curr[1])
        v2 = (p_next[0] - p_curr[0], p_next[1] - p_curr[1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 == 0 or l2 == 0 or r <= 0:
            out.append(p_curr)
            continue

        # Clamp radius so it doesn't exceed half of either adjacent edge
        rr = min(r, l1 / 2, l2 / 2)
        a = (p_curr[0] + v1[0] / l1 * rr, p_curr[1] + v1[1] / l1 * rr)
        b = (p_curr[0] + v2[0] / l2 * rr, p_curr[1] + v2[1] / l2 * rr)

        # Quadratic bezier a → (p_curr) → b, sampled as a polyline
        for s in range(segments + 1):
            t = s / segments
            x = (1 - t) ** 2 * a[0] + 2 * (1 - t) * t * p_curr[0] + t ** 2 * b[0]
            y = (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * p_curr[1] + t ** 2 * b[1]
            out.append((x, y))
    return out


def main(output_paths):
    img = Image.new("RGBA", (W, W), (*BG_NAVY, 255))

    # 1. Subtle radial glow from the center (slate-900-ish → slate-950)
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    glow_r = int(W * 0.55)
    for i in range(glow_r, 0, -8):
        a = int(18 * (1 - i / glow_r))
        gd.ellipse((CX - i, CY - i, CX + i, CY + i), fill=(*CYAN_400, a))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(radius=30 * SSAA // 4)))

    # 2. Three concentric radar arcs, decreasing opacity outward
    arc_specs = [
        # radius (px at 1x), stroke (1x), alpha (0-255)
        (170, 18, 255),
        (290, 16, 200),
        (410, 14, 140),
    ]
    for r_base, w_base, a in arc_specs:
        draw_arc_circle(
            img, CX, CY,
            radius=r_base * SSAA,
            width=w_base * SSAA,
            color=CYAN_400, alpha=a,
        )

    # 3. Center filled dot
    dot_r = 46 * SSAA
    dot = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(dot)
    dd.ellipse((CX - dot_r, CY - dot_r, CX + dot_r, CY + dot_r), fill=(*CYAN_400, 255))
    img.alpha_composite(dot)

    # 4. Arrow — symmetric about the canvas center, running lower-left to
    # upper-right. Midpoint sits on the center dot for "current from here".
    tail = (CX - 220 * SSAA, CY + 265 * SSAA)
    tip  = (CX + 220 * SSAA, CY - 265 * SSAA)
    shaft_w = 78 * SSAA
    head_w  = 200 * SSAA
    head_len = 150 * SSAA

    poly = arrow_polygon(tail, tip, shaft_w, head_w, head_len)

    # Per-vertex corner radii (supersampled units). Tail vertices get a radius
    # close to half the shaft so they round into a pill cap; tip and flare
    # corners get moderate rounding; shaft/head junction gets a gentle curve.
    sx = SSAA  # alias — already baked into all other values
    radii = [
        40 * sx,  # 0: tail left (full cap)
        16 * sx,  # 1: base left (shaft)
        30 * sx,  # 2: base left (head flare outer)
        26 * sx,  # 3: tip (slightly blunt)
        30 * sx,  # 4: base right (head flare outer)
        16 * sx,  # 5: base right (shaft)
        40 * sx,  # 6: tail right (full cap)
    ]
    poly_rounded = round_polygon(poly, radii)

    # Cyan glow: draw the exact arrow silhouette in cyan, then blur heavily.
    # A Gaussian blur spreads uniformly in every direction, so the halo is
    # consistent around tip, tail, and sides — no manual per-edge inflation.
    glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gl = ImageDraw.Draw(glow_layer)
    gl.polygon(poly_rounded, fill=(*CYAN_400, 255))
    # Layered bloom accumulated additively (ImageChops.add) so each pass
    # actually brightens the halo rather than flat-compositing under it.
    far_bloom   = glow_layer.filter(ImageFilter.GaussianBlur(radius=150 * SSAA // 4))
    outer_bloom = glow_layer.filter(ImageFilter.GaussianBlur(radius=95  * SSAA // 4))
    mid_halo    = glow_layer.filter(ImageFilter.GaussianBlur(radius=50  * SSAA // 4))
    tight_halo  = glow_layer.filter(ImageFilter.GaussianBlur(radius=22  * SSAA // 4))

    glow_accum = Image.new("RGBA", img.size, (0, 0, 0, 0))
    for passes, layer in [(3, far_bloom), (3, outer_bloom), (2, mid_halo), (1, tight_halo)]:
        for _ in range(passes):
            glow_accum = ImageChops.add(glow_accum, layer)
    img.alpha_composite(glow_accum)

    # White arrow on top
    arrow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    al = ImageDraw.Draw(arrow_layer)
    al.polygon(poly_rounded, fill=(*WHITE, 255))
    img.alpha_composite(arrow_layer)

    # Downsample
    final = img.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)

    for path in output_paths:
        final.save(path, "PNG", optimize=True)
        print(f"wrote {path}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: generate_icon.py <output_path> [<output_path> ...]")
        sys.exit(1)
    main(sys.argv[1:])
