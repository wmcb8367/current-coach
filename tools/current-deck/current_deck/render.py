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
    vmax_kt: float      # speed at the top of the colour gradient
    ramp: tuple         # gradient stops, weakest to strongest

    def fmt(self, kt: float) -> str:
        v = kt * self.factor
        return f"{v:.2f} kt" if self.name == "kt" else f"{v:.1f} m/min"

    def frac(self, kt: float) -> float:
        return min(max(kt / self.vmax_kt, 0.0), 1.0)

    def color(self, kt: float) -> str:
        return ramp_color(self.ramp, self.frac(kt))

    def ticks(self):
        """(fraction, label) pairs for the key: evenly spaced values in display units."""
        top = self.vmax_kt * self.factor
        step = next(st for st in (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100) if top / st <= 5.01)
        out, v = [], 0.0
        while v <= top + 1e-9:
            out.append((v / top, f"{v:g}"))
            v += step
        return out


def ramp_color(ramp, t: float) -> str:
    """Linear interpolation along the gradient stops; t in 0..1."""
    from matplotlib.colors import to_hex, to_rgb
    x = min(max(t, 0.0), 1.0) * (len(ramp) - 1)
    i = min(int(x), len(ramp) - 2)
    a, b = to_rgb(ramp[i]), to_rgb(ramp[i + 1])
    k = x - i
    return to_hex([a[j] + (b[j] - a[j]) * k for j in range(3)])


def make_units(name: str, ramp, max_kt: float, top: float | None = None) -> Units:
    """`top` = top of the colour scale in display units; default rounds the strongest reading up."""
    factor = M_PER_NM / 60 if name == "m/min" else 1.0
    if top is None:
        step = 5.0 if name == "m/min" else 0.25
        top = max(step, math.ceil(max_kt * factor / step - 1e-9) * step)
    return Units(name, factor, top / factor, tuple(ramp))


# --------------------------------------------------------------------------- geometry
@dataclass
class MapFrame:
    lat0: float
    lon0: float
    kt_to_nm: float          # arrow length in nm per knot of current
    xlim: tuple
    ylim: tuple
    base_nm: float = 0.0     # length every arrow gets before speed is added, so weak ones keep a tail

    @property
    def coslat(self) -> float:
        return math.cos(math.radians(self.lat0))

    def xy(self, lat: float, lon: float):
        return ((lon - self.lon0) * NM_PER_DEG * self.coslat, (lat - self.lat0) * NM_PER_DEG)

    def latlon(self, x: float, y: float):
        return (self.lat0 + y / NM_PER_DEG, self.lon0 + x / (NM_PER_DEG * self.coslat))

    def arrow(self, m: Measurement):
        x, y = self.xy(m.lat, m.lon)
        L = self.base_nm + m.speed_kt * self.kt_to_nm
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


BASE_ARROW_IN = 0.30      # arrow length at zero speed, inches on the slide
SHAFT_IN = 0.062          # shaft thickness
HEAD_LEN_IN = 0.21        # arrowhead length (shrinks a little on the shortest arrows)
HEAD_W_IN = 0.20          # arrowhead width


def build_frame(ms: list[Measurement], aspect: float, *, panel_w_in: float = RIGHT_PANEL[2], **kw) -> MapFrame:
    """Fit all readings and their arrows in a box with width/height = aspect."""
    f = _build_frame(ms, aspect, 0.0, **kw)
    for _ in range(2):      # base arrow length is fixed in inches, so it depends on the fitted width
        f = _build_frame(ms, aspect, BASE_ARROW_IN * f.width / panel_w_in, **kw)
    return f


def _build_frame(ms: list[Measurement], aspect: float, base_nm: float, *, arrow_frac: float = 0.3,
                 kt_to_nm: float | None = None, pad_frac: float = 0.12, margin_nm: float = 0.0) -> MapFrame:
    lat0 = sum(m.lat for m in ms) / len(ms)
    lon0 = sum(m.lon for m in ms) / len(ms)
    f = MapFrame(lat0, lon0, 1.0, (0, 1), (0, 1), base_nm)
    pts = [f.xy(m.lat, m.lon) for m in ms]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.5)
    vmax = max(max(m.speed_kt for m in ms), 0.2)
    f.kt_to_nm = kt_to_nm if kt_to_nm else arrow_frac * span / vmax
    for m in ms:
        _, (hx, hy) = f.arrow(m)
        xs.append(hx)
        ys.append(hy)
    pad = pad_frac * max(max(xs) - min(xs), max(ys) - min(ys), 0.3) + margin_nm
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad
    # readings live in the middle band: the key takes the top 15%, scale bar the bottom 14%
    H = (y1 - y0) / 0.71
    y0, y1 = y0 - 0.14 * H, y1 + 0.15 * H
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
    return MapFrame(zoom.lat0, zoom.lon0, zoom.kt_to_nm, xl, yl, zoom.base_nm)


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
    L = _nice(0.25 * f.width * M_PER_NM / 2.4, steps=(1, 2, 5))     # between ~10% and 25% of the width
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


KEY_BOX = (0.022, 0.895, 0.46, 0.075)      # x, y, w, h in axes fractions (top-left of the panel)


def legend(ax, th: Theme, units: Units, fs=1.0):
    """Slim pill-shaped gradient key with the unit at the left and values printed on the bar."""
    import matplotlib.patheffects as pe
    x, y, w, h = KEY_BOX
    pos = ax.get_position()
    ratio = (pos.height * ax.figure.get_figheight()) / (pos.width * ax.figure.get_figwidth())
    pill = FancyBboxPatch((x + h * ratio / 2, y + h / 2), w - h * ratio, 0, transform=ax.transAxes,
                          boxstyle=f"round,pad={h / 2},rounding_size={h / 2}", mutation_aspect=1 / ratio * 1.0,
                          fc="none", ec=th.text, lw=0.8, alpha=0.9, zorder=11)
    pill.set_boxstyle("round", pad=h * ratio / 2, rounding_size=h * ratio / 2)
    pill.set_mutation_aspect(1 / ratio)
    ax.add_patch(pill)
    lead = 0.17           # share of the bar before the 0 tick, holding the unit label
    n = 120
    for i in range(n):
        t = i / (n - 1)
        r = Rectangle((x + w * i / n, y), w / n * 1.3, h, transform=ax.transAxes, ec="none", zorder=10,
                      fc=ramp_color(units.ramp, max(0.0, (t - lead) / (1 - lead - 0.07))))
        ax.add_patch(r)
        r.set_clip_path(pill)
    stroke = [pe.withStroke(linewidth=2.2 * fs, foreground="#00000066")]
    kw = dict(transform=ax.transAxes, color="#FFFFFF", fontsize=12.5 * fs, fontfamily=th.mono,
              fontweight="bold", va="center", ha="center", zorder=12, path_effects=stroke)
    ax.text(x + w * lead * 0.45, y + h / 2, units.name, **kw)
    for t, lab in units.ticks():
        ax.text(x + w * (lead + t * (1 - lead - 0.07)), y + h / 2, lab, **kw)


def arrow_polygon(tail, head, unit_nm: float, fs: float = 1.0):
    """Arrow outline with a real shaft. `unit_nm` = map nm per slide inch."""
    dx, dy = head[0] - tail[0], head[1] - tail[1]
    L = math.hypot(dx, dy) or 1e-9
    ux, uy = dx / L, dy / L
    px, py = -uy, ux
    hl = min(HEAD_LEN_IN * fs * unit_nm, 0.6 * L)
    hw = HEAD_W_IN * fs * unit_nm * (hl / (HEAD_LEN_IN * fs * unit_nm)) ** 0.5 / 2
    sw = SHAFT_IN * fs * unit_nm / 2
    nx, ny = head[0] - ux * hl, head[1] - uy * hl      # where the head starts
    return [(tail[0] + px * sw, tail[1] + py * sw), (nx + px * sw, ny + py * sw), (nx + px * hw, ny + py * hw),
            head, (nx - px * hw, ny - py * hw), (nx - px * sw, ny - py * sw), (tail[0] - px * sw, tail[1] - py * sw)]


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
            for dist in (1.0, 2.2, 3.6):
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
        self.last_box = best
        return (best[0] + best[2]) / 2, (best[1] + best[3]) / 2


def draw_reading(ax, f: MapFrame, th: Theme, m: Measurement, units: Units, alpha: float = 1.0, *,
                 label: str | None = None, placer: LabelPlacer | None = None, fs: float = 1.0,
                 arrow: bool = True):
    """Arrow from the reading toward the set, white dot at the reading, bold label."""
    if alpha <= 0.01:
        return []
    c = units.color(m.speed_kt)
    tail, head = f.arrow(m)
    arts = []
    pos = ax.get_position()
    unit_nm = f.width / (pos.width * ax.figure.get_figwidth()) / fs   # nm per slide inch
    if arrow:
        arr = Polygon(arrow_polygon(tail, head, unit_nm, fs), closed=True, fc=c, ec=th.panel, lw=0.8 * fs,
                      alpha=alpha, zorder=5, joinstyle="miter")
        ax.add_patch(arr)
        arts.append(arr)
    arts.append(ax.scatter([tail[0]], [tail[1]], s=70 * fs, color="#FFFFFF", alpha=alpha,
                           edgecolors=th.panel, linewidths=1.6 * fs, zorder=6))
    if label:
        if placer is None:
            placer = LabelPlacer(ax, f, fs)
            placer.add_segment(tail, head)
        lx, ly = placer.place(tail, m.toward_deg, label)
        # thin leader from the reading to its label when the label had to move away
        bx0, by0, bx1, by1 = placer.last_box
        ex, ey = min(max(tail[0], bx0), bx1), min(max(tail[1], by0), by1)
        gap_in = math.hypot((ex - tail[0]) / f.width * placer.w_in, (ey - tail[1]) / f.height * placer.h_in)
        if gap_in > 0.2 * fs:
            arts += ax.plot([tail[0], ex], [tail[1], ey], color=th.text, lw=0.9 * fs, alpha=0.55 * alpha, zorder=4)
        txt = ax.text(lx, ly, label, color=th.text, alpha=alpha, fontsize=13 * fs,
                      fontfamily=th.mono, fontweight="bold", ha="center", va="center", zorder=7,
                      clip_on=True)
        if not arrow:      # keep labels readable over the particle trails
            import matplotlib.patheffects as pe
            txt.set_path_effects([pe.withStroke(linewidth=3 * fs, foreground=th.panel, alpha=0.9)])
        arts.append(txt)
    return arts


def legend_keepout(f: MapFrame):
    """Data-unit box covered by legend() (left half of the width, top 13% of the height)."""
    return (f.xlim[0], f.ylim[0] + 0.87 * f.height, f.xlim[0] + 0.51 * f.width, f.ylim[1])


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


def parse_window(text) -> float:
    """'90', '90m', '3h', '1h30m' -> minutes; 'all' / 'day' -> infinity (readings never expire)."""
    import re
    t = str(text).strip().lower()
    if t in ("all", "day", "all-day", "allday", "forever", "inf"):
        return math.inf
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes)?)?", t)
    if not m or not (m[1] or m[2]):
        raise ValueError(f"cannot read a time window from {text!r}; use minutes (90), 90m, 3h, 1h30m or all")
    minutes = float(m[1] or 0) * 60 + float(m[2] or 0)
    if minutes <= 0:
        raise ValueError("the time window must be longer than zero")
    return minutes


def parse_distance(text) -> float:
    """'800', '800m', '1.5km', '0.5nm' -> metres."""
    import re
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|km|nm|nmi|mi)?", str(text).strip().lower())
    if not m or float(m[1]) <= 0:
        raise ValueError(f"cannot read a distance from {text!r}; use metres (800), 800m, 1.5km or 0.5nm")
    return float(m[1]) * {"m": 1.0, "km": 1000.0, "nm": M_PER_NM, "nmi": M_PER_NM, "mi": 1609.344}[m[2] or "m"]


def fmt_window(minutes: float) -> str:
    if math.isinf(minutes):
        return "all day"
    if minutes >= 120 and minutes % 30 == 0:
        return f"{minutes / 60:g} h"
    return f"{minutes:g} min"


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
               step_min: float | None = None, frame_ms: int = 90, scale: float = 0.96, max_frames: int = 400,
               style: str = "arrows", reach_m: float = 1000.0, particles: int = 1400):
    """Animated scrubber: each reading appears at its time and is gone `lifetime` minutes later.

    style="arrows" draws one arrow per reading. style="particles" draws drifting particles
    through a field interpolated from the live readings, limited to `reach_m` from each one.
    """
    register_fonts()
    t_start = _floor(ms[0].t - timedelta(minutes=5), 5)
    t_end = ms[-1].t + timedelta(minutes=30 if math.isinf(lifetime) else lifetime)
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
    reach_txt = f"{reach_m / 1000:g} km" if reach_m >= 1000 else f"{reach_m:g} m"
    foot = (f"{zone_label} · flow shown only within {reach_txt} of a reading" if style == "particles"
            else f"{zone_label} · arrows fade out {lifetime:g} min after each reading")
    if math.isinf(lifetime):
        foot = (f"{zone_label} · readings stay all day · reach {reach_txt}" if style == "particles"
                else f"{zone_label} · arrows stay for the rest of the day")
    elif style == "particles":
        foot = f"{zone_label} · readings stay {fmt_window(lifetime)} · reach {reach_txt}"
    axc.text(0.06, 0.14, foot,
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
        axs.plot([x, x], [0.55, 1.0], color=units.color(m.speed_kt), lw=2, zorder=3)

    field = None
    if style == "particles":
        fade = min(fade, lifetime / 6) if not math.isinf(lifetime) else fade
        from .particles import ParticleField, reach_outline
        field = ParticleField(zoom, units, reach_m=reach_m, n=particles)
    dyn, frames = [], []
    for i in range(n):
        for a in dyn:
            a.remove()
        dyn = []
        now = t_start + timedelta(minutes=i * step_min)
        shown = [(m, fade_alpha((now - m.t).total_seconds() / 60, lifetime, fade)) for m in ms]
        shown = [(m, al) for m, al in shown if al > 0]
        live = len(shown)
        if field is not None:
            field.step(shown)
            dyn.append(field.draw(axr, fs))
            dyn += reach_outline(axr, zoom, shown, field.reach, th.muted, fs)
        placer = make_placer(axr, zoom, [m for m, _ in shown] if field is None else [], fs)
        if field is not None:           # no arrows to avoid, but keep labels off the reading dots
            for m, _ in shown:
                dot = zoom.xy(m.lat, m.lon)
                placer.add_segment(dot, dot)
        for m, al in sorted(shown, key=lambda p: p[0].t):
            dyn += draw_reading(axr, zoom, th, m, units, al, placer=placer, fs=fs, arrow=field is None,
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
    info = {"frames": n, "step_min": round(step_min, 2), "start": t_start.isoformat(), "end": t_end.isoformat(),
            "style": style, "lifetime_min": None if math.isinf(lifetime) else lifetime}
    if style == "particles":
        info["reach_m"] = reach_m
    return info


def render_average_gif(ms: list[Measurement], th: Theme, units: Units, path, *, kicker: str, title: str,
                       zone_label: str, zoom: MapFrame, land, reach_m: float = 1000.0, frames_n: int = 70,
                       frame_ms: int = 90, scale: float = 0.96, particles: int = 2400):
    """Looping particle view of the whole day: every reading counts for the whole animation.

    Where readings overlap, the flow is their distance-weighted average. Same bounded reach as
    the scrubber, so nothing is drawn where nothing was measured.
    """
    from .particles import ParticleField, reach_outline
    register_fonts()
    fig = plt.figure(figsize=(SW * scale, SH * scale), dpi=100, facecolor=th.page)
    fs = scale
    header(fig, th, kicker, title)
    box = (LEFT_PANEL[0], LEFT_PANEL[1], LEFT_PANEL[2], 3.05)
    loc = locator_frame(zoom, box[2] / box[3])
    axl = _axes(fig, box)
    draw_panel(axl, loc, th, land)
    race_box(axl, zoom, loc, th, fs)
    north_arrow(axl, th, fs, x=0.07, y=0.82)
    scale_bar(axl, loc, th, fs)

    axc = _axes(fig, (LEFT_PANEL[0], 4.85, LEFT_PANEL[2], 1.75))
    axc.set_facecolor(th.box)
    axc.set_xticks([])
    axc.set_yticks([])
    for sp in axc.spines.values():
        sp.set_color(th.border)
        sp.set_linewidth(1.6)
    mean = sum(m.speed_kt for m in ms) / len(ms)
    reach_txt = f"{reach_m / 1000:g} km" if reach_m >= 1000 else f"{reach_m:g} m"
    kw = dict(transform=axc.transAxes, fontfamily=th.mono, va="center")
    axc.text(0.06, 0.84, "ALL-DAY AVERAGE", color=th.kicker, fontsize=12, fontweight="bold", **kw)
    axc.text(0.06, 0.52, f"{ms[0].t:%H:%M} to {ms[-1].t:%H:%M}", transform=axc.transAxes, color=th.text,
             fontsize=30, fontfamily=th.sans, fontweight="bold", va="center")
    axc.text(0.06, 0.27, f"{len(ms)} readings · mean {units.fmt(mean)}", color=th.text, fontsize=11.5, **kw)
    axc.text(0.06, 0.12, f"{zone_label} · flow shown only within {reach_txt} of a reading", color=th.muted,
             fontsize=9.5, **kw)

    axr = _axes(fig, RIGHT_PANEL)
    draw_panel(axr, zoom, th, land)
    legend(axr, th, units, fs)
    scale_bar(axr, zoom, th, fs)
    shown = [(m, 1.0) for m in ms]
    reach_outline(axr, zoom, shown, reach_m / M_PER_NM, th.muted, fs)
    xs, ys = zip(*[zoom.xy(m.lat, m.lon) for m in ms])
    axr.scatter(xs, ys, s=34 * fs, color="#FFFFFF", edgecolors=th.panel, linewidths=1.2 * fs, zorder=6)

    fig.text(SCRUBBER[0] / SW, 1 - (SCRUBBER[1] + 0.2) / SH,
             "Every reading of the day at once. Where readings overlap, speed and direction are averaged, "
             "whatever the time between them.", color=th.faint, fontsize=11, fontfamily=th.mono, va="center")

    field = ParticleField(zoom, units, reach_m=reach_m, n=particles)
    for _ in range(field.life):          # warm up so the first frame is already full
        field.step(shown)
    frames, lc = [], None
    for _ in range(frames_n):
        if lc is not None:
            lc.remove()
        field.step(shown)
        lc = field.draw(axr, fs)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=th.page)
        buf.seek(0)
        frames.append(Image.open(buf).convert("RGB"))
    plt.close(fig)
    _save_gif(frames, path, frame_ms, hold=False)
    return {"frames": frames_n, "readings": len(ms), "reach_m": reach_m}


def _save_gif(frames, path, frame_ms, hold: bool = True):
    # One shared palette, built from a sample of frames, keeps colours stable between frames.
    sample = frames[:: max(1, len(frames) // 8)]
    w, h = sample[0].size
    sheet = Image.new("RGB", (w, h * len(sample)))
    for k, im in enumerate(sample):
        sheet.paste(im, (0, h * k))
    pal = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    q = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in frames]
    durations = [frame_ms] * len(q)
    if hold:
        durations[0] = 700
        durations[-1] = 2500
    q[0].save(path, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=False, disposal=1)
