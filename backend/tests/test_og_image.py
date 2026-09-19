"""The preview image a shared link unfurls into (app/og_image.py).

No golden files: text rasterises differently across FreeType builds. The tests
decode the PNG and look at pixels whose colour the layout fixes — inside a bar
segment, in the empty track, inside the verdict pill.
"""

from __future__ import annotations

import time
from io import BytesIO

import pytest
from PIL import Image

from app import og_image
from app.og_image import BAR_BOTTOM, BAR_TOP, BAR_WIDTH, HEIGHT, LEFT, RIGHT, WIDTH, render
from app.share import card
from tests.factories import make_election, make_forecast

HASH = "3f" * 32
BAR_MIDDLE = (BAR_TOP + BAR_BOTTOM) // 2


def rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(ch * 2 for ch in hex_color)
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def draw(election, selection, seats_claimed=None) -> Image.Image:
    png = render(card(election, HASH, selection, seats_claimed))
    image = Image.open(BytesIO(png))
    assert image.format == "PNG"
    return image


def bar_x(seats: float, total_seats: int) -> int:
    return LEFT + round(seats / total_seats * BAR_WIDTH)


def pill_centre_y() -> int:
    return round(332 + og_image.font(96, bold=True).getmetrics()[0] - 34)


def test_the_image_is_a_1200_by_630_rgb_png():
    image = draw(make_election(), [0])
    assert image.size == (WIDTH, HEIGHT) == (1200, 630)
    assert image.mode == "RGB"


def test_a_pixel_inside_the_first_segment_has_that_partys_colour():
    # make_election: L has 6 of 10 seats in #C0392B, R the other 4.
    image = draw(make_election(), [0])
    assert image.getpixel((bar_x(3, 10), BAR_MIDDLE)) == rgb("#C0392B")


def test_segments_follow_one_another_in_list_order():
    image = draw(make_election(), [0, 1])
    assert image.getpixel((bar_x(3, 10), BAR_MIDDLE)) == rgb("#C0392B")
    assert image.getpixel((bar_x(8, 10), BAR_MIDDLE)) == rgb("#2980B9")


def test_the_empty_remainder_of_the_bar_is_the_grey_track():
    image = draw(make_election(), [0])
    assert image.getpixel((bar_x(8.5, 10), BAR_MIDDLE)) == rgb("#EEEEEE")


def test_with_nothing_selected_the_whole_bar_is_track():
    image = draw(make_election(), [])
    for seats in (0.5, 3, 8.5):
        assert image.getpixel((bar_x(seats, 10), BAR_MIDDLE)) == rgb("#EEEEEE")


def test_three_digit_party_colours_are_drawn_too():
    blocks = [{"name": "All", "parties": [
        {"name": "Short", "abbr": "S", "seats": 6, "color": "#0a0"},
        {"name": "Other", "abbr": "O", "seats": 4, "color": "#123456"},
    ]}]
    image = draw(make_election(blocks=blocks), [0])
    assert image.getpixel((bar_x(3, 10), BAR_MIDDLE)) == rgb("#00AA00")


def test_the_majority_marker_crosses_the_bar():
    # 60 seats of 100 to a majority of 51: the marker falls inside the segment.
    blocks = [{"name": "All", "parties": [
        {"name": "Big", "abbr": "B", "seats": 60, "color": "#C0392B"},
        {"name": "Rest", "abbr": "R", "seats": 40, "color": "#2980B9"},
    ]}]
    image = draw(make_election(total_seats=100, majority_seats=51, blocks=blocks), [0])
    assert image.getpixel((bar_x(51, 100), BAR_MIDDLE)) == rgb("#111111")


@pytest.mark.parametrize("selection, background", [
    ([1], "#F0F0F0"),     # 4 of 10: short
    ([0], "#E6F1FB"),     # 6 of 10: a majority
    ([0, 1], "#EAF3DE"),  # 10 of 10: more than two thirds
])
def test_the_pill_takes_the_verdicts_colours(selection, background):
    image = draw(make_election(), selection)
    assert image.getpixel((RIGHT - 8, pill_centre_y())) == rgb(background)


def test_the_longest_text_and_the_most_parties_the_schema_allows_still_render_quickly():
    blocks = [
        {"name": "B" * 200, "parties": [
            {"name": "N" * 200, "abbr": chr(0x41 + b % 26) * 200, "seats": 1, "color": "#abcdef"}
            for _ in range(200)
        ]}
        for b in range(50)
    ]
    election = make_forecast(
        title="W" * 200, publisher="P" * 200, total_seats=10_000, majority_seats=5_001, blocks=blocks,
    )
    started = time.perf_counter()
    image = draw(election, list(range(10_000)))
    assert time.perf_counter() - started < 2
    assert image.size == (1200, 630)


def test_a_title_with_glyphs_the_font_lacks_still_renders():
    image = draw(make_election(title="選挙 2026 ✓ ★"), [0])
    assert image.size == (1200, 630)


def test_ellipsize_keeps_text_that_fits_and_cuts_text_that_does_not():
    face = og_image.font(26)
    assert og_image.ellipsize("Folketing", face, 500) == "Folketing"
    cut = og_image.ellipsize("Folketing " * 50, face, 300)
    assert cut.endswith("…")
    assert face.getlength(cut) <= 300
