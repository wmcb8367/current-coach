"""Matplotlib rendering: map panels, hourly panel images and the scrubber GIF.

Layout is in slide inches on a 13.333 x 7.5 in (16:9) canvas so the GIF on
slide 1 and the hourly slides line up exactly.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle  # noqa: E402
from PIL import Image  # noqa: E402

from .loader import Measurement  # noqa: E402
from .style import Theme, register_fonts  # noqa: E402

NM_PER_DEG = 60.0
M_PER_NM = 1852.0
SW, SH = 13.333, 7.5                    # slide size, inches

# Panel boxes in slide inches: (left, top, width, height)
LEFT_PANEL = (0.67, 1.62, 4.45, 4.98)
RIGHT_PANEL = (5.40, 1.62, 7.27, 4.98)
GIF_LOCATOR = (0.67, 1.62, 4.45, 3.05)
GIF_CLOCK = (0.67, 4.85, 4.45, 1.75)
SCRUBBER = (0.67, 6.80, 12.0, 0.50)


# --------------------------------------------------------------------------- units
@dataclass(frozen=True)
class Units:
    name: str           # "kt" or "m/min"
    factor: float       # display value per knot
    bins: tuple         # 3 thresholds in display units -> 4 classes

    def fmt(self, kt: float) -> str:
        v = kt * self.factor
        return f"{v:.2f} kt" if self.name == "kt" else f"{v:.1f} m/min"

    def cls(self, kt: float) -> int:
        v = kt * self.factor
        return sum(v >= b for b in self.bins)

    def legend(self):
        b = self.bins
        u = self.name
        return [f"Under {b[0]:g} {u}", f"{b[0]:g} to {b[1]:g} {u}", f"{b[1]:g} to {b[2]:g} {u}",
                f"{b[2]:g} {u} and up"]


def make_units(name: str = "kt", bins=None) -> Units:
    if name == "m/min":
        return Units("m/min", M_PER_NM / 60, tuple(bins) if bins else (8.0, 15.0, 23.0))
    return Units("kt", 1.0, tuple(bins) if bins else (0.25, 0.5, 0.75))


# --------------------------------------------------------------------------- geometry
@dataclass
class MapFrame:
    lat0: float
    lon0: float
    kt_to_nm: float          # arrow length in nm per knot of current
    xlim: tuple
    ylim: tuple

    @property
    def coslat(self) -> float:
        return math.cos(math.radians(self.lat0))

    def xy(self, lat: float, lon: float):
        return ((lon - self.lon0) * NM_PER_DEG * self.coslat, (lat - self.lat0) * NM_PER_DEG)

    def latlon(self, x: float, y: float):
        return (self.lat0 + y / NM_PER_DEG, self.lon0 + x / (NM_PER_DEG * self.coslat))

    def arrow(self, m: Measurement):
        x, y = self.xy(m.lat, m.lon)
        L = m.speed_kt * self.kt_to_nm
        a = math.radians(m.toward_deg)
        return (x, y), (x + L * math.sin(a), y + L * math.cos(a))

    @property
    def width(self):
        return self.xlim[1] - self.xlim[0]

    @property
    def height(self):
        return self.ylim[1] - self.ylim[0]

    def bbox_lonlat(self, grow: float = 0.0):
        la0, lo0 = self.latlon(self.xlim[0] - grow * self.width, self.ylim[0] - grow * self.height)
        la1, lo1 = self.latlon(self.xlim[1] + grow * self.width, self.ylim[1] + grow * self.height)
        return (lo0, la0, lo1, la1)


def _fit(x0, x1, y0, y1, aspect):
    w, h = x1 - x0, y1 - y0
    if w / h < aspect:
        g = (h * aspect - w) / 2
        return (x0 - g, x1 + g), (y0, y1)
    g = (w / aspect - h) / 2
    return (x0, x1), (y0 - g, y1 + g)


def build_frame(ms: list[Measurement], aspect: float, *, arrow_frac: float = 0.4,
                kt_to_nm: float | None = None, pad_frac: float = 0.12) -> MapFrame:
    """Fit all readings and their arrows in a box with width/height = aspect."""
    lat0 = sum(m.lat for m in ms) / len(ms)
    lon0 = sum(m.lon for m in ms) / len(ms)
    f = MapFrame(lat0, lon0, 1.0, (0, 1), (0, 1))
    pts = [f.xy(m.lat, m.lon) for m in ms]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.5)
    vmax = max(max(m.speed_kt for m in ms), 0.2)
    f.kt_to_nm = kt_to_nm if kt_to_nm else arrow_frac * span / vmax
    for m in ms:
        _, (hx, hy) = f.arrow(m)
        xs.append(hx)
        ys.append(hy)
    pad = pad_frac * max(max(xs) - min(xs), max(ys) - min(ys), 0.3)
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad
    # readings live in the lower-middle band: legend takes the top 36%, scale bar the bottom 14%
    H = (y1 - y0) / 0.50
    y0, y1 = y0 - 0.14 * H, y1 + 0.36 * H
    W = x1 - x0
    if W / H < aspect:
        g = (H * aspect - W) / 2
        x0, x1 = x0 - g, x1 + g
    else:
        # wider than the panel: grow height, keeping the same top/bottom split
        extra = W / aspect - H
        y0, y1 = y0 - 0.5 * extra, y1 + 0.5 * extra
    f.xlim, f.ylim = (x0, x1), (y0, y1)
    return f


def locator_frame(zoom: MapFrame, aspect: float, factor: float = 4.0, min_span_nm: float = 3.0) -> MapFrame:
    cx, cy = sum(zoom.xlim) / 2, sum(zoom.ylim) / 2
    span = max(max(zoom.width, zoom.height) * factor, min_span_nm)
    xl, yl = _fit(cx - span / 2, cx + span / 2, cy - span / 2, cy + span / 2, aspect)
    return MapFrame(zoom.lat0, zoom.lon0, zoom.kt_to_nm, xl, yl)


def _nice(v: float, steps=(1, 2, 5)) -> float:
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for s in steps + (10,):
        if s * e >= v * 0.999:
            return s * e
    return 10 * e


def _axes(fig, box, page_w=SW, page_h=SH):
    l, t, w, h = box
    return fig.add_axes([l / page_w, 1 - (t + h) / page_h, w / page_w, h / page_h])


# --------------------------------------------------------------------------- panel pieces
def draw_panel(ax, f: MapFrame, th: Theme, land):
    ax.set_facecolor(th.panel)
    ax.set_xlim(*f.xlim)
    ax.set_ylim(*f.ylim)
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color(th.border)
        s.set_linewidth(1.6)
    for ring in land or []:
        pts = [f.xy(lat, lon) for lon, lat in ring]
        ax.add_patch(Polygon(pts, closed=True, fc=th.land, ec=th.coast, lw=0.8, zorder=1))


def scale_bar(ax, f: MapFrame, th: Theme, fs=1.0):
    L = _nice(0.16 * f.width * M_PER_NM, steps=(1, 2, 5))
    label = f"{L / 1000:g} km" if L >= 1000 else f"{L:g} m"
    x0 = f.xlim[0] + 0.05 * f.width
    y0 = f.ylim[0] + 0.055 * f.height
    ax.plot([x0, x0 + L / M_PER_NM], [y0, y0], color=th.text, lw=3 * fs, solid_capstyle="butt", zorder=8)
    ax.text(x0, y0 + 0.035 * f.height, label, color=th.text, fontsize=13 * fs, fontfamily=th.mono,
            va="bottom", ha="left", zorder=8)


def north_arrow(ax, th: Theme, fs=1.0, x=0.08, y=0.86):
    ax.add_patch(Polygon([(x, y + 0.07), (x - 0.03, y - 0.02), (x, y + 0.005), (x + 0.03, y - 0.02)],
                         closed=True, transform=ax.transAxes, fc=th.text, ec="none", zorder=9))
    ax.text(x, y - 0.06, "N", transform=ax.transAxes, color=th.text, fontsize=13 * fs,
            fontfamily=th.mono, fontweight="bold", ha="center", va="center", zorder=9)


def legend(ax, th: Theme, units: Units, fs=1.0):
    rows = units.legend()
    x, y = 0.022, 0.975
    lh = 0.062
    h = lh * (len(rows) + 1) + 0.03
    ax.add_patch(FancyBboxPatch((x, y - h), 0.43, h, boxstyle="square,pad=0", transform=ax.transAxes,
                                fc=th.box, ec=th.border, lw=1, zorder=9))
    ax.text(x + 0.025, y - 0.045, "CURRENT STRENGTH", transform=ax.transAxes, color=th.kicker,
            fontsize=12.5 * fs, fontfamily=th.mono, fontweight="bold", va="center", zorder=10)
    for i, r in enumerate(rows):
        yy = y - 0.045 - lh * (i + 1)
        ax.add_patch(Rectangle((x + 0.025, yy - 0.017), 0.035, 0.034, transform=ax.transAxes,
                               fc=th.strength[i], ec=th.text, lw=0.5, zorder=10))
        ax.text(x + 0.075, yy, r, transform=ax.transAxes, color=th.text, fontsize=12 * fs,
                fontfamily=th.mono, va="center", zorder=10)


class LabelPlacer:
    """Greedy label placement: try spots around each reading and keep the one that
    overlaps the fewest other labels, arrows, the legend and the panel edge."""

    def __init__(self, ax, f: MapFrame, fs: float = 1.0, keepout=None):
        fig = ax.figure
        pos = ax.get_position()
        self.w_in = pos.width * fig.get_figwidth()
        self.h_in = pos.height * fig.get_figheight()
        self.f, self.fs = f, fs
        self.boxes = list(keepout or [])        # (x0, y0, x1, y1) in data units

    def add_segment(self, a, b, r_in=0.09):
        rx, ry = r_in * self.f.width / self.w_in, r_in * self.f.height / self.h_in
        for k in range(6):
            t = k / 5
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            self.boxes.append((x - rx, y - ry, x + rx, y + ry))

    def place(self, tail, toward_deg, text, size_pt=13):
        f = self.f
        w = len(text) * 0.6 * size_pt * self.fs / 72 * f.width / self.w_in
        h = 1.35 * size_pt * self.fs / 72 * f.height / self.h_in
        gx, gy = 0.12 * f.width / self.w_in, 0.10 * f.height / self.h_in
        a = math.radians(toward_deg)
        away = (-math.sin(a), -math.cos(a))
        cands = []
        for dx, dy in [away, (1, 0), (-1, 0), (0, -1), (0, 1), (0.7, -0.7), (-0.7, -0.7), (0.7, 0.7), (-0.7, 0.7)]:
            for dist in (1.0, 2.2):
                cx = tail[0] + dx * (gx * dist + w / 2)
                cy = tail[1] + dy * (gy * dist + h / 2)
                cands.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
        (x0, x1), (y0, y1) = f.xlim, f.ylim

        def cost(b, i):
            c = 0.0
            for o in self.boxes:
                ox = max(0, min(b[2], o[2]) - max(b[0], o[0]))
                oy = max(0, min(b[3], o[3]) - max(b[1], o[1]))
                c += ox * oy
            out = (max(0, x0 - b[0]) + max(0, b[2] - x1)) * h + (max(0, y0 - b[1]) + max(0, b[3] - y1)) * w
            return c + 5 * out + i * 1e-4 * w * h

        best = min(enumerate(cands), key=lambda ib: cost(ib[1], ib[0]))[1]
        self.boxes.append(best)
        return (best[0] + best[2]) / 2, (best[1] + best[3]) / 2


def draw_reading(ax, f: MapFrame, th: Theme, m: Measurement, units: Units, alpha: float = 1.0, *,
                 label: str | None = None, placer: LabelPlacer | None = None, fs: float = 1.0):
    """Arrow from the reading toward the set, white dot at the reading, bold label."""
    if alpha <= 0.01:
        return []
    c = th.strength[units.cls(m.speed_kt)]
    tail, head = f.arrow(m)
    arts = []
    arr = FancyArrowPatch(tail, head, arrowstyle="-|>,head_length=0.7,head_width=0.45",
                          mutation_scale=26 * fs, lw=4.2 * fs, color=c, alpha=alpha, zorder=5,
                          shrinkA=0, shrinkB=0, joinstyle="miter", capstyle="butt")
    ax.add_patch(arr)
    arts.append(arr)
    arts.append(ax.scatter([tail[0]], [tail[1]], s=70 * fs, color="#FFFFFF", alpha=alpha,
                           edgecolors=th.panel, linewidths=1.6 * fs, zorder=6))
    if label:
        if placer is None:
            placer = LabelPlacer(ax, f, fs)
            placer.add_segment(tail, head)
        lx, ly = placer.place(tail, m.toward_deg, label)
        arts.append(ax.text(lx, ly, label, color=th.text, alpha=alpha, fontsize=13 * fs,
                            fontfamily=th.mono, fontweight="bold", ha="center", va="center", zorder=7,
                            clip_on=True))
    return arts


def legend_keepout(f: MapFrame):
    """Data-unit box covered by legend() (axes fraction 0.022..0.452 x, top ~0.33 of height)."""
    return (f.xlim[0], f.ylim[0] + 0.62 * f.height, f.xlim[0] + 0.47 * f.width, f.ylim[1])


def scale_keepout(f: MapFrame):
    return (f.xlim[0], f.ylim[0], f.xlim[0] + 0.3 * f.width, f.ylim[0] + 0.16 * f.height)


def make_placer(ax, f, ms_shown, fs=1.0):
    p = LabelPlacer(ax, f, fs, keepout=[legend_keepout(f), scale_keepout(f)])
    for m in ms_shown:
        p.add_segment(*f.arrow(m))
    return p


def race_box(ax, zoom: MapFrame, loc: MapFrame, th: Theme, fs=1.0):
    (x0, x1), (y0, y1) = zoom.xlim, zoom.ylim
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, ec=th.wordmark, lw=2.4 * fs,
                           ls=(0, (3, 2)), zorder=6))
    below, va = y0 - 0.03 * loc.height, "top"
    if below < loc.ylim[0] + 0.08 * loc.height:
        below, va = y1 + 0.03 * loc.height, "bottom"
    ax.text(x0, below, "RACE AREA, SHOWN RIGHT", color=th.wordmark, fontsize=12 * fs, fontfamily=th.mono,
            fontweight="bold", va=va, ha="left", zorder=7, clip_on=True)


def ghost_dots(ax, f, th, ms, fs=1.0):
    if ms:
        gx, gy = zip(*[f.xy(m.lat, m.lon) for m in ms])
        ax.scatter(gx, gy, s=22 * fs, color=th.muted, alpha=0.35, linewidths=0, zorder=4)


# --------------------------------------------------------------------------- hourly panels
def render_hour_panels(ms_hour, all_ms, zoom: MapFrame, loc: MapFrame, th: Theme, units: Units,
                       land, path, *, dpi=170):
    """One PNG covering both panels, placed on the slide at LEFT_PANEL's top-left corner."""
    register_fonts()
    l0, t0 = LEFT_PANEL[0], LEFT_PANEL[1]
    W = RIGHT_PANEL[0] + RIGHT_PANEL[2] - l0
    H = LEFT_PANEL[3]
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=th.page)

    def box(b):
        return (b[0] - l0, b[1] - t0, b[2], b[3])

    axl = _axes(fig, box(LEFT_PANEL), W, H)
    draw_panel(axl, loc, th, land)
    race_box(axl, zoom, loc, th)
    north_arrow(axl, th)
    scale_bar(axl, loc, th)

    axr = _axes(fig, box(RIGHT_PANEL), W, H)
    draw_panel(axr, zoom, th, land)
    ghost_dots(axr, zoom, th, [m for m in all_ms if m not in ms_hour])
    placer = make_placer(axr, zoom, ms_hour)
    for m in ms_hour:
        draw_reading(axr, zoom, th, m, units, 1.0, label=f"{m.t:%H:%M} · {units.fmt(m.speed_kt)}", placer=placer)
    legend(axr, th, units)
    scale_bar(axr, zoom, th)
    fig.savefig(path, facecolor=th.page)
    plt.close(fig)
    return (W, H)


# --------------------------------------------------------------------------- GIF
def fade_alpha(age_min: float, lifetime: float, fade: float) -> float:
    if age_min < 0 or age_min >= lifetime:
        return 0.0
    if age_min <= lifetime - fade:
        return 1.0
    return max(0.0, (lifetime - age_min) / fade)


def _floor(dt: datetime, minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % minutes)


def header(fig, th: Theme, kicker: str, title: str):
    fig.text(0.67 / SW, 1 - 0.55 / SH, kicker.upper(), color=th.kicker, fontsize=15, fontfamily=th.mono,
             fontweight="bold", va="top", ha="left")
    fig.text(0.67 / SW, 1 - 0.88 / SH, title, color=th.text, fontsize=34, fontfamily=th.sans,
             fontweight="bold", va="top", ha="left")
    fig.text(12.67 / SW, 1 - 0.55 / SH, th.wordmark_text, color=th.wordmark, fontsize=24,
             fontfamily=th.mono, fontweight="bold", va="top", ha="right")


def render_gif(ms: list[Measurement], th: Theme, units: Units, path, *, kicker: str, title: str,
               zone_label: str, zoom: MapFrame, land, lifetime: float = 60, fade: float = 20,
               step_min: float | None = None, frame_ms: int = 90, scale: float = 0.96, max_frames: int = 400):
    """Animated scrubber: each arrow appears at its time and is gone `lifetime` minutes later."""
    register_fonts()
    t_start = _floor(ms[0].t - timedelta(minutes=5), 5)
    t_end = ms[-1].t + timedelta(minutes=lifetime)
    span_min = (t_end - t_start).total_seconds() / 60
    if step_min is None:
        step_min = max(1.0, round(span_min / 150))
    n = max(2, min(int(span_min / step_min) + 1, max_frames))
    step_min = span_min / (n - 1)

    fig = plt.figure(figsize=(SW * scale, SH * scale), dpi=100, facecolor=th.page)
    fs = scale
    header(fig, th, kicker, title)

    loc = locator_frame(zoom, GIF_LOCATOR[2] / GIF_LOCATOR[3])
    axl = _axes(fig, GIF_LOCATOR)
    draw_panel(axl, loc, th, land)
    race_box(axl, zoom, loc, th, fs)
    north_arrow(axl, th, fs, x=0.07, y=0.82)
    scale_bar(axl, loc, th, fs)

    axc = _axes(fig, GIF_CLOCK)
    axc.set_facecolor(th.box)
    axc.set_xticks([])
    axc.set_yticks([])
    for s in axc.spines.values():
        s.set_color(th.border)
        s.set_linewidth(1.6)
    axc.text(0.06, 0.84, "TIME ON THE WATER", transform=axc.transAxes, color=th.kicker, fontsize=12,
             fontfamily=th.mono, fontweight="bold", va="center")
    axc.text(0.06, 0.14, f"{zone_label} · arrows fade out {lifetime:g} min after each reading",
             transform=axc.transAxes, color=th.muted, fontsize=9.5, fontfamily=th.mono, va="center")

    axr = _axes(fig, RIGHT_PANEL)
    draw_panel(axr, zoom, th, land)
    ghost_dots(axr, zoom, th, ms, fs)
    legend(axr, th, units, fs)
    scale_bar(axr, zoom, th, fs)

    axs = _axes(fig, SCRUBBER)
    axs.set_xlim(-span_min * 0.01, span_min * 1.01)
    axs.set_ylim(-1, 1)
    axs.axis("off")
    axs.plot([0, span_min], [0.25, 0.25], color=th.border, lw=6, solid_capstyle="round", zorder=1)
    hour = t_start.replace(minute=0)
    while hour <= t_end:
        x = (hour - t_start).total_seconds() / 60
        if 0 <= x <= span_min:
            axs.plot([x, x], [-0.05, 0.55], color=th.muted, lw=1, zorder=2)
            axs.text(x, -0.6, f"{hour:%H:%M}", color=th.muted, fontsize=10, fontfamily=th.mono,
                     ha="center", va="center")
        hour += timedelta(hours=1)
    for m in ms:
        x = (m.t - t_start).total_seconds() / 60
        axs.plot([x, x], [0.55, 1.0], color=th.strength[units.cls(m.speed_kt)], lw=2, zorder=3)

    dyn, frames = [], []
    for i in range(n):
        for a in dyn:
            a.remove()
        dyn = []
        now = t_start + timedelta(minutes=i * step_min)
        shown = [(m, fade_alpha((now - m.t).total_seconds() / 60, lifetime, fade)) for m in ms]
        shown = [(m, al) for m, al in shown if al > 0]
        live = len(shown)
        placer = make_placer(axr, zoom, [m for m, _ in shown], fs)
        for m, al in sorted(shown, key=lambda p: p[0].t):
            dyn += draw_reading(axr, zoom, th, m, units, al, placer=placer, fs=fs,
                                label=f"{m.t:%H:%M} · {units.fmt(m.speed_kt)}")
        x = i * step_min
        dyn += axs.plot([0, x], [0.25, 0.25], color=th.wordmark, lw=6, solid_capstyle="round", zorder=4)
        dyn.append(axs.scatter([x], [0.25], s=170, color=th.wordmark, edgecolors=th.page, linewidths=2, zorder=5))
        dyn.append(axc.text(0.06, 0.48, f"{now:%H:%M}", transform=axc.transAxes, color=th.text, fontsize=54,
                            fontfamily=th.sans, fontweight="bold", va="center"))
        dyn.append(axc.text(0.64, 0.48, f"{live} reading{'s' if live != 1 else ''}\nshowing",
                            transform=axc.transAxes, color=th.muted, fontsize=12, fontfamily=th.mono,
                            va="center", linespacing=1.4))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=th.page)
        buf.seek(0)
        frames.append(Image.open(buf).convert("RGB"))
    plt.close(fig)
    _save_gif(frames, path, frame_ms)
    return {"frames": n, "step_min": round(step_min, 2), "start": t_start.isoformat(), "end": t_end.isoformat()}


def _save_gif(frames, path, frame_ms):
    # One shared palette, built from a sample of frames, keeps colours stable between frames.
    sample = frames[:: max(1, len(frames) // 8)]
    w, h = sample[0].size
    sheet = Image.new("RGB", (w, h * len(sample)))
    for k, im in enumerate(sample):
        sheet.paste(im, (0, h * k))
    pal = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    q = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in frames]
    durations = [frame_ms] * len(q)
    durations[0] = 700
    durations[-1] = 2500
    q[0].save(path, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=False, disposal=1)
