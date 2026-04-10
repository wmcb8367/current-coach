#!/usr/bin/env python3
"""Generate Current Coach app icon — ocean current with directional arrow."""

from PIL import Image, ImageDraw, ImageFont
import math

SIZE = 1024
CENTER = SIZE // 2

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# --- Background: deep ocean gradient (simulated with concentric rects) ---
for y in range(SIZE):
    t = y / SIZE
    # Deep blue at top -> teal at bottom
    r = int(0 + 10 * t)
    g = int(60 + 100 * t)
    b = int(160 + 60 * t)
    draw.line([(0, y), (SIZE - 1, y)], fill=(r, g, b, 255))

# --- Rounded corners mask ---
mask = Image.new("L", (SIZE, SIZE), 0)
mask_draw = ImageDraw.Draw(mask)
radius = int(SIZE * 0.22)
mask_draw.rounded_rectangle([0, 0, SIZE, SIZE], radius=radius, fill=255)

# --- Wave patterns (subtle, layered) ---
for wave_idx in range(4):
    y_base = 300 + wave_idx * 160
    alpha = 30 - wave_idx * 5
    for x in range(0, SIZE, 2):
        phase = wave_idx * 0.8
        y = y_base + int(25 * math.sin(x * 0.012 + phase)) + int(10 * math.sin(x * 0.025 + phase * 2))
        for dy in range(3):
            py = y + dy
            if 0 <= py < SIZE:
                existing = img.getpixel((x, py))
                blended_r = min(255, existing[0] + alpha)
                blended_g = min(255, existing[1] + alpha)
                blended_b = min(255, existing[2] + alpha)
                img.putpixel((x, py), (blended_r, blended_g, blended_b, 255))
                if x + 1 < SIZE:
                    existing2 = img.getpixel((x + 1, py))
                    img.putpixel((x + 1, py), (min(255, existing2[0] + alpha), min(255, existing2[1] + alpha), min(255, existing2[2] + alpha), 255))

# --- Main current arrow (large, bold, pointing upper-right ~45°) ---
arrow_overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
arrow_draw = ImageDraw.Draw(arrow_overlay)

# Arrow parameters
angle = -45  # degrees, pointing upper-right
arrow_len = 420
shaft_width = 70
head_width = 180
head_len = 160

cx, cy = CENTER + 20, CENTER + 20  # slightly offset from center

# Arrow direction vector
rad = math.radians(angle)
dx = math.cos(rad)
dy = math.sin(rad)
# Perpendicular
px, py_perp = -dy, dx

# Shaft start and end
sx, sy = cx - dx * arrow_len * 0.45, cy - dy * arrow_len * 0.45
ex, ey = cx + dx * arrow_len * 0.45, cy + dy * arrow_len * 0.45

# Shaft polygon (rectangle along the arrow)
shaft_hw = shaft_width / 2
shaft_points = [
    (sx + px * shaft_hw, sy + py_perp * shaft_hw),
    (sx - px * shaft_hw, sy - py_perp * shaft_hw),
    (ex - px * shaft_hw, ey - py_perp * shaft_hw),
    (ex + px * shaft_hw, ey + py_perp * shaft_hw),
]
arrow_draw.polygon(shaft_points, fill=(255, 255, 255, 220))

# Arrowhead (triangle)
head_hw = head_width / 2
tip_x = ex + dx * head_len
tip_y = ey + dy * head_len
head_points = [
    (tip_x, tip_y),
    (ex + px * head_hw, ey + py_perp * head_hw),
    (ex - px * head_hw, ey - py_perp * head_hw),
]
arrow_draw.polygon(head_points, fill=(255, 255, 255, 240))

# --- Glow effect behind arrow ---
glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
glow_draw = ImageDraw.Draw(glow)
for i in range(20, 0, -1):
    alpha_g = int(8 * (20 - i))
    expand = i * 3
    glow_shaft = [
        (sx + px * (shaft_hw + expand), sy + py_perp * (shaft_hw + expand)),
        (sx - px * (shaft_hw + expand), sy - py_perp * (shaft_hw + expand)),
        (ex - px * (shaft_hw + expand), ey - py_perp * (shaft_hw + expand)),
        (ex + px * (shaft_hw + expand), ey + py_perp * (shaft_hw + expand)),
    ]
    glow_draw.polygon(glow_shaft, fill=(100, 220, 255, min(alpha_g, 40)))

img = Image.alpha_composite(img, glow)
img = Image.alpha_composite(img, arrow_overlay)

# --- Small secondary arrow (smaller, showing a second current reading) ---
arrow2_overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
arrow2_draw = ImageDraw.Draw(arrow2_overlay)

angle2 = -30
arrow_len2 = 200
shaft_w2 = 35
head_w2 = 90
head_l2 = 80

cx2, cy2 = CENTER - 140, CENTER + 160
rad2 = math.radians(angle2)
dx2, dy2 = math.cos(rad2), math.sin(rad2)
px2, py2 = -dy2, dx2

sx2, sy2 = cx2 - dx2 * arrow_len2 * 0.45, cy2 - dy2 * arrow_len2 * 0.45
ex2, ey2 = cx2 + dx2 * arrow_len2 * 0.45, cy2 + dy2 * arrow_len2 * 0.45

shaft2 = [
    (sx2 + px2 * shaft_w2 / 2, sy2 + py2 * shaft_w2 / 2),
    (sx2 - px2 * shaft_w2 / 2, sy2 - py2 * shaft_w2 / 2),
    (ex2 - px2 * shaft_w2 / 2, ey2 - py2 * shaft_w2 / 2),
    (ex2 + px2 * shaft_w2 / 2, ey2 + py2 * shaft_w2 / 2),
]
arrow2_draw.polygon(shaft2, fill=(130, 230, 180, 180))

tip2_x = ex2 + dx2 * head_l2
tip2_y = ey2 + dy2 * head_l2
head2 = [
    (tip2_x, tip2_y),
    (ex2 + px2 * head_w2 / 2, ey2 + py2 * head_w2 / 2),
    (ex2 - px2 * head_w2 / 2, ey2 - py2 * head_w2 / 2),
]
arrow2_draw.polygon(head2, fill=(130, 230, 180, 200))

img = Image.alpha_composite(img, arrow2_overlay)

# --- Compass ring (subtle, top-right area) ---
compass_overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
compass_draw = ImageDraw.Draw(compass_overlay)

comp_cx, comp_cy = SIZE - 200, 200
comp_r = 80
for a in range(0, 360, 30):
    r_a = math.radians(a)
    inner = comp_r - 8
    outer = comp_r
    if a % 90 == 0:
        inner = comp_r - 16
        w = 3
    else:
        w = 2
    x1 = comp_cx + inner * math.sin(r_a)
    y1 = comp_cy - inner * math.cos(r_a)
    x2 = comp_cx + outer * math.sin(r_a)
    y2 = comp_cy - outer * math.cos(r_a)
    compass_draw.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 100), width=w)

# Compass circle
compass_draw.ellipse(
    [comp_cx - comp_r, comp_cy - comp_r, comp_cx + comp_r, comp_cy + comp_r],
    outline=(255, 255, 255, 80), width=2
)

img = Image.alpha_composite(img, compass_overlay)

# Flatten to RGB (no alpha) - App Store requires opaque icons
# Skip rounded corners (iOS applies them automatically)
output = Image.new("RGB", (SIZE, SIZE), (0, 60, 160))
output.paste(img.convert("RGB"), (0, 0))

# Save
output_path = "/Users/mcbrideagents/Projects/current-coach/CurrentCoach/Resources/Assets.xcassets/AppIcon.appiconset/icon_1024.png"
output.save(output_path, "PNG")

print(f"Icon saved to {output_path}")
print(f"Size: {output.size}")
