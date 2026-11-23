"""Build the PPTX: slide 1 is the animated scrubber GIF, then one slide per hour."""

from __future__ import annotations

from itertools import groupby

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from .loader import Measurement
from .render import LEFT_PANEL, SH, SW, Units
from .style import Theme


def _rgb(hexstr: str) -> RGBColor:
    return RGBColor.from_string(hexstr.lstrip("#").upper())


def _bg(slide, color):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(color)


def _runs(slide, x, y, w, h, parts, *, size, font, bold=False, spacing=None, align=PP_ALIGN.LEFT):
    """Text box with one paragraph made of (text, colour) runs."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = align
    for text, color in parts:
        r = p.add_run()
        r.text = text
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.name = font
        r.font.color.rgb = _rgb(color)
        if spacing is not None:
            r._r.get_or_add_rPr().set("spc", str(int(spacing * 100)))
    return tb


def header(slide, th: Theme, kicker: str, title: str):
    _runs(slide, 0.67, 0.5, 9.5, 0.35, [(kicker.upper(), th.kicker)], size=15, font=th.mono, bold=True, spacing=4)
    _runs(slide, 0.67, 0.82, 9.5, 0.8, [(title, th.text)], size=36, font=th.sans, bold=True)
    _runs(slide, 8.67, 0.5, 4.0, 0.5, [(th.wordmark_text, th.wordmark)], size=24, font=th.mono, bold=True,
          align=PP_ALIGN.RIGHT)


def hour_groups(ms: list[Measurement]):
    return [(h, list(g)) for h, g in groupby(ms, key=lambda m: m.t.replace(minute=0, second=0, microsecond=0))]


def build_deck(path, *, th: Theme, units: Units, gif_path, hour_panels: dict, ms: list[Measurement],
               title: str, kicker: str, zone_label: str, sources: list[str], lifetime: float,
               coach_note: str | None = None, average_gif=None, lifetime_label: str | None = None):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SW), Inches(SH)
    blank = prs.slide_layouts[6]

    # ---- slide 1: animated scrubber (the GIF carries its own header)
    s = prs.slides.add_slide(blank)
    _bg(s, th.page)
    s.shapes.add_picture(str(gif_path), 0, 0, width=prs.slide_width, height=prs.slide_height)
    s.notes_slide.notes_text_frame.text = (
        f"{title}. Time scrubber of {len(ms)} current readings. Each arrow appears at its reading time and "
        f"stays for {lifetime_label or f'{lifetime:g} minutes'}. Arrows point the way the water flows. Times {zone_label}. "
        f"Source: {', '.join(sources)}")

    # ---- slide 2: all-day average (looping particles)
    if average_gif is not None:
        s = prs.slides.add_slide(blank)
        _bg(s, th.page)
        s.shapes.add_picture(str(average_gif), 0, 0, width=prs.slide_width, height=prs.slide_height)
        s.notes_slide.notes_text_frame.text = (
            f"All-day average of {len(ms)} current readings, {ms[0].t:%H:%M} to {ms[-1].t:%H:%M} {zone_label}. "
            "Every reading counts for the whole animation; where their reach overlaps, speed and direction "
            f"are averaged regardless of time. Source: {', '.join(sources)}")

    # ---- one slide per hour
    for hour, group in hour_groups(ms):
        s = prs.slides.add_slide(blank)
        _bg(s, th.page)
        end = hour.replace(minute=59)
        header(s, th, f"{kicker} · {hour:%H}:00 {zone_label}", f"Current readings {hour:%H:%M} to {end:%H:%M}")
        img, (w, h) = hour_panels[hour]
        s.shapes.add_picture(str(img), Inches(LEFT_PANEL[0]), Inches(LEFT_PANEL[1]), Inches(w), Inches(h))

        n = len(group)
        strong = max(group, key=lambda m: m.speed_kt)
        bits = [f"{n} reading{'s' if n != 1 else ''}"]
        if n > 1:
            mean = sum(m.speed_kt for m in group) / n
            bits.append(f"strongest {units.fmt(strong.speed_kt)} toward {strong.toward_deg:03.0f}°T at {strong.t:%H:%M}")
            bits.append(f"mean {units.fmt(mean)}")
        else:
            bits.append(f"{units.fmt(strong.speed_kt)} toward {strong.toward_deg:03.0f}°T at {strong.t:%H:%M}")
        if coach_note:
            bits.append(coach_note)
        bits.append("arrows point where the water flows")
        _runs(s, 0.67, 6.86, 12.0, 0.4, [("● ", th.faint), (" · ".join(bits), th.faint)], size=13, font=th.mono)

        s.notes_slide.notes_text_frame.text = "\n".join(
            f"{m.t:%H:%M}  {units.fmt(m.speed_kt)}  toward {m.toward_deg:03.0f}°T (from {m.from_deg:03.0f}°)"
            + (f"  wind {m.wind_from:03.0f}° {m.wind_kt:g} kt" if m.wind_from is not None and m.wind_kt is not None else "")
            + (f"  {m.note}" if m.note else "") for m in group) + f"\nSource: {', '.join(sources)}"
    prs.save(str(path))
    return len(prs.slides)
