"""The preview image a shared link unfurls into: 1200×630 PNG, drawn with Pillow.

Link previews want a raster image of about this size, and most platforms ignore
SVG, so the backend draws one from a :class:`app.share.Card` — the same card
the page's ``<meta>`` tags are worded from. Layout and colours follow
``doc/plans/02-share-links.md``: title and subtitle, the seat bar with the
selected parties and the majority marker, the total with the page's verdict
pill, and a chip per selected party.

The fonts are Noto Sans Regular and Bold (SIL Open Font License, ``fonts/``).
Glyphs they lack draw as boxes. The one the layout itself needs, the tick in
``Majority ✓``, is not in Noto Sans, so it is drawn as two strokes instead.

Every input is bounded before it gets here — at most 200 characters of text
per field and as many parties as the schema allows — and each piece of text is
cut to the width it has, so one render is tens of milliseconds whatever the
election.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .share import Card

WIDTH, HEIGHT = 1200, 630
DOMAIN = "koalitionsberegner.moritzmarcus.com"

FONTS = Path(__file__).resolve().parent / "fonts"

LEFT, RIGHT = 64, 1136
BAR_TOP, BAR_BOTTOM = 200, 284
BAR_WIDTH = RIGHT - LEFT
TRACK = "#EEEEEE"

#: The page's verdict colours (``index.html``): text on background.
PILLS = {
    "short": ("#666666", "#F0F0F0"),
    "majority": ("#185FA5", "#E6F1FB"),
    "large": ("#3B6D11", "#EAF3DE"),
}

#: At most this wide, however long an abbreviation is.
MAX_CHIP_TEXT = 480
CHIP_ROWS = (470, 522)
CHIP_DOT, CHIP_DOT_GAP, CHIP_GAP = 14, 10, 32


@lru_cache(maxsize=None)
def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf"
    return ImageFont.truetype(str(FONTS / name), size)


def render(card: Card) -> bytes:
    """The card as PNG bytes."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    _header(draw, card)
    _bar(image, draw, card)
    _total(draw, card)
    _chips(draw, card)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def ellipsize(text: str, face: ImageFont.FreeTypeFont, width: float) -> str:
    """``text``, cut and ended with an ellipsis where it is wider than ``width``."""
    if face.getlength(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:  # the longest prefix that still fits beside the ellipsis
        middle = (low + high + 1) // 2
        if face.getlength(text[:middle].rstrip() + "…") <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _header(draw: ImageDraw.ImageDraw, card: Card) -> None:
    title_face, domain_face = font(44, bold=True), font(22)
    baseline = 56 + title_face.getmetrics()[0]
    draw.text((RIGHT, baseline), DOMAIN, font=domain_face, fill="#888888", anchor="rs")
    # The title stops short of the domain rather than running under it.
    room = min(1000, RIGHT - domain_face.getlength(DOMAIN) - 32 - LEFT)
    draw.text((LEFT, baseline), ellipsize(card.title, title_face, room),
              font=title_face, fill="#111111", anchor="ls")
    subtitle_face = font(26)
    draw.text((LEFT, 124), ellipsize(card.subtitle, subtitle_face, BAR_WIDTH),
              font=subtitle_face, fill="#666666")


def _bar(image: Image.Image, draw: ImageDraw.ImageDraw, card: Card) -> None:
    height = BAR_BOTTOM - BAR_TOP
    # Segments are drawn on their own strip and pasted through the track's
    # rounded shape, so the first and last segment follow its corners.
    strip = Image.new("RGB", (BAR_WIDTH, height), TRACK)
    strip_draw = ImageDraw.Draw(strip)
    seats_before = 0
    for _abbr, seats, color in card.parties:
        # Edges from cumulative seats, so rounding never opens a gap.
        x0 = round(seats_before / card.total_seats * BAR_WIDTH)
        seats_before += seats
        x1 = round(seats_before / card.total_seats * BAR_WIDTH)
        if x1 > x0:
            strip_draw.rectangle((x0, 0, x1 - 1, height - 1), fill=color)
    mask = Image.new("L", strip.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, BAR_WIDTH - 1, height - 1), radius=12, fill=255)
    image.paste(strip, (LEFT, BAR_TOP), mask)

    marker = LEFT + round(card.majority_seats / card.total_seats * BAR_WIDTH)
    marker = min(marker, RIGHT - 2)
    draw.rectangle((marker - 1, BAR_TOP - 8, marker + 1, BAR_BOTTOM + 8), fill="#111111")
    label, label_face = f"Majority {card.majority_seats}", font(18)
    half = label_face.getlength(label) / 2
    centre = min(max(marker, LEFT + half), RIGHT - half)
    draw.text((centre, BAR_BOTTOM + 12), label, font=label_face, fill="#111111", anchor="ma")


def _total(draw: ImageDraw.ImageDraw, card: Card) -> None:
    number_face, of_face = font(96, bold=True), font(32)
    baseline = 332 + number_face.getmetrics()[0]
    number = str(card.total)
    draw.text((LEFT, baseline), number, font=number_face, fill="#111111", anchor="ls")
    of_x = LEFT + number_face.getlength(number) + 16
    draw.text((of_x, baseline), f"of {card.total_seats} seats", font=of_face, fill="#333333", anchor="ls")
    _pill(draw, card, centre_y=baseline - 34)


def _pill(draw: ImageDraw.ImageDraw, card: Card, *, centre_y: float) -> None:
    face = font(30)
    ink, background = PILLS[card.verdict]
    if card.verdict == "short":
        parts = [f"{card.majority_seats - card.total} short"]
    elif card.verdict == "majority":
        parts = ["Majority", "✓", f"+{card.total - card.majority_seats}"]
    else:
        parts = ["Large majority", "✓"]

    tick, gap = 22, face.getlength(" ")
    widths = [tick if part == "✓" else face.getlength(part) for part in parts]
    inner = sum(widths) + gap * (len(parts) - 1)
    pad_x, height = 24, 56
    left, top = RIGHT - inner - 2 * pad_x, centre_y - height / 2
    draw.rounded_rectangle((left, top, RIGHT, top + height), radius=height / 2, fill=background)

    x = left + pad_x
    for part, width in zip(parts, widths):
        if part == "✓":
            points = [(x + 1, centre_y + 1), (x + 8, centre_y + 8), (x + tick - 1, centre_y - 9)]
            draw.line(points, fill=ink, width=4, joint="curve")
        else:
            draw.text((x, centre_y), part, font=face, fill=ink, anchor="lm")
        x += width + gap


def _chips(draw: ImageDraw.ImageDraw, card: Card) -> None:
    face = font(26)
    if not card.parties:
        draw.text((LEFT, CHIP_ROWS[0]), "Open the link and pick parties.", font=face, fill="#666666")
        return

    # Chips are measured only until two rows' worth have been: an election of
    # ten thousand parties costs no more to draw than one of ten.
    chips, measured = [], 0.0
    for abbr, seats, color in card.parties:
        if measured > len(CHIP_ROWS) * BAR_WIDTH:
            break
        label = ellipsize(abbr, face, MAX_CHIP_TEXT)
        seats_text = f" {seats}"
        width = CHIP_DOT + CHIP_DOT_GAP + face.getlength(label) + face.getlength(seats_text)
        chips.append((label, seats_text, color, width))
        measured += width + CHIP_GAP

    rows = _wrap([chip[3] for chip in chips], len(card.parties), face)
    hidden = len(card.parties) - sum(len(row) for row in rows)
    for row, (y, indices) in enumerate(zip(CHIP_ROWS, rows)):
        x = LEFT
        centre = y + face.getmetrics()[0] * 0.62
        for i in indices:
            label, seats_text, color, width = chips[i]
            draw.ellipse((x, centre - CHIP_DOT / 2, x + CHIP_DOT, centre + CHIP_DOT / 2), fill=color)
            text_x = x + CHIP_DOT + CHIP_DOT_GAP
            draw.text((text_x, y), label, font=face, fill="#111111")
            draw.text((text_x + face.getlength(label), y), seats_text, font=face, fill="#666666")
            x += width + CHIP_GAP
        if row == len(rows) - 1 and hidden:
            draw.text((x, y), f"+{hidden} more", font=face, fill="#666666")


def _wrap(widths: list[float], count: int, face: ImageFont.FreeTypeFont) -> list[list[int]]:
    """Chip indices per row: at most two rows, with room left for ``+N more``.

    ``widths`` are the first chips' widths; ``count`` is how many chips there are.
    """
    rows: list[list[int]] = [[]]
    x = 0.0
    for i, width in enumerate(widths):
        if rows[-1] and x + width > BAR_WIDTH:
            if len(rows) == len(CHIP_ROWS):
                break
            rows.append([])
            x = 0.0
        rows[-1].append(i)
        x += width + CHIP_GAP

    # Not every chip fits: drop chips off the last row until the note does.
    while rows[-1]:
        hidden = count - sum(len(row) for row in rows)
        if not hidden:
            break
        used = sum(widths[i] + CHIP_GAP for i in rows[-1])
        if used + face.getlength(f"+{hidden} more") <= BAR_WIDTH:
            break
        rows[-1].pop()
    return rows
