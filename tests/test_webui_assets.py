"""Static checks for the dashboard assets.

These guard a class of bug that no Python test could catch before: the page
toggles panels with the ``hidden`` attribute, but the UA stylesheet rule
``[hidden] { display: none }`` loses to any author rule that sets ``display``
on the same element.  When that happened to ``.ms-drawer`` the detail drawer
stayed on screen from the first paint and its full-screen scrim absorbed every
click, making the whole dashboard unusable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE_DIR = Path(__file__).resolve().parent.parent / "pages" / "memory"
HTML = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
CSS = (PAGE_DIR / "style.css").read_text(encoding="utf-8")
JS = (PAGE_DIR / "app.js").read_text(encoding="utf-8")

HIDDEN_RESET = re.compile(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important")
TAG_RE = re.compile(r"<(\w+)([^>]*)>")


def _tags_with_hidden_attribute() -> list[str]:
    found = []
    for _tag, attrs in TAG_RE.findall(HTML):
        if re.search(r"(?:^|\s)hidden(?:\s|=|$)", attrs):
            found.append(attrs)
    return found


def _classes(attrs: str) -> list[str]:
    match = re.search(r"class=\"([^\"]*)\"", attrs)
    return match.group(1).split() if match else []


def _display_rules_for(css_class: str) -> list[str]:
    pattern = re.compile(r"(?<![-\w])\." + re.escape(css_class) + r"(?![-\w])[^{}]*\{([^}]*)\}")
    return [body for body in pattern.findall(CSS) if re.search(r"(?<!-)display\s*:", body)]


def test_hidden_attribute_is_reset_with_important() -> None:
    assert HIDDEN_RESET.search(CSS), (
        "style.css must reset [hidden] with display: none !important, otherwise "
        "class-level display rules keep hidden elements visible"
    )


def test_page_uses_the_hidden_attribute() -> None:
    # If the page ever stops using the attribute this suite would pass vacuously.
    assert _tags_with_hidden_attribute()
    assert ".hidden = " in JS or "hidden = false" in JS


@pytest.mark.parametrize("attrs", _tags_with_hidden_attribute())
def test_hidden_elements_are_actually_hidden(attrs: str) -> None:
    for css_class in _classes(attrs):
        conflicting = _display_rules_for(css_class)
        if conflicting:
            assert HIDDEN_RESET.search(CSS), (
                f".{css_class} sets display ({conflicting!r}) and would override "
                "the hidden attribute"
            )


def test_drawer_scrim_cannot_block_the_page_when_closed() -> None:
    # The scrim is fixed and covers the viewport, so a stuck-open drawer is fatal.
    assert 'id="drawer"' in HTML
    drawer_tag = re.search(r"<div id=\"drawer\"([^>]*)>", HTML)
    assert drawer_tag is not None
    assert re.search(r"(?:^|\s)hidden(?:\s|>|$)", drawer_tag.group(1)), (
        "the drawer must start hidden"
    )
    assert HIDDEN_RESET.search(CSS)


def test_element_ids_used_by_the_script_exist_in_the_markup() -> None:
    ids_in_html = set(re.findall(r"id=\"([^\"]+)\"", HTML))
    ids_in_js = set(re.findall(r"\bel\(\"([^\"]+)\"\)", JS))
    dynamic = {"detail-chart"}  # created by renderDetail at runtime
    missing = sorted(ids_in_js - ids_in_html - dynamic)
    assert not missing, f"app.js looks up ids that index.html does not define: {missing}"
