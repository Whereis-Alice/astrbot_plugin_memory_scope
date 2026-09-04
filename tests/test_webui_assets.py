"""Static checks for the dashboard assets.

These guard a class of bug that no Python test could catch before: the page
toggles panels with the ``hidden`` attribute, but the UA stylesheet rule
``[hidden] { display: none }`` loses to any author rule that sets ``display``
on the same element.  When that happened to ``.ms-drawer`` the detail drawer
stayed on screen from the first paint and its full-screen scrim absorbed every
click, making the whole dashboard unusable.
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# i18n coverage
#
# t() falls back to the Chinese literal passed at the call site, so a missing
# translation key is invisible in zh-CN and silently shows Chinese in en-US.
# These checks treat the call-site literals as the source of truth and hold the
# two JSON bundles to them.

I18N_DIR = Path(__file__).resolve().parent.parent / ".astrbot-plugin" / "i18n"
ZH = json.loads((I18N_DIR / "zh-CN.json").read_text(encoding="utf-8"))
EN = json.loads((I18N_DIR / "en-US.json").read_text(encoding="utf-8"))

#: t("key", "fallback") with both arguments written out literally.
T_CALL_RE = re.compile(r"\bt\(\s*\"((?:[^\"\\]|\\.)+)\"\s*,\s*\"((?:[^\"\\]|\\.)*)\"\s*\)")
#: The COLUMNS table stores the same pair under named fields.
COLUMN_RE = re.compile(r"i18n:\s*\"([^\"]+)\"\s*,\s*fallback:\s*\"([^\"]*)\"")
#: <span data-i18n="key">中文</span>; applyStaticText() uses the inner text as
#: the fallback, so nested markup is skipped rather than mis-parsed.
DATA_I18N_RE = re.compile(r"<\w+[^>]*\bdata-i18n=\"([^\"]+)\"[^>]*>([^<]*)<")

#: Keys assembled at runtime from a prefix plus a code, e.g. t("note." + note).
#: They cannot appear as literals, so they are exempt from the orphan check.
DYNAMIC_PREFIXES = ("skin.", "bucket.", "note.", "alerts.", "help.")


def _flatten(node: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _page_keys(bundle: dict) -> dict[str, str]:
    return _flatten(bundle.get("pages", {}).get("memory", {}))


def _static_pairs() -> dict[str, str]:
    """Every literal key -> Chinese fallback used by the page."""

    pairs: dict[str, str] = {}
    for source, matches in (
        (JS, T_CALL_RE.findall(JS)),
        (JS, COLUMN_RE.findall(JS)),
        (HTML, DATA_I18N_RE.findall(HTML)),
    ):
        del source
        for key, fallback in matches:
            pairs[key] = fallback.strip()
    return pairs


ZH_KEYS = _page_keys(ZH)
EN_KEYS = _page_keys(EN)
STATIC_PAIRS = _static_pairs()


def test_the_page_actually_declares_translation_keys() -> None:
    # Guards against the regexes silently matching nothing after a refactor.
    assert len(T_CALL_RE.findall(JS)) > 150
    assert len(DATA_I18N_RE.findall(HTML)) > 20
    assert len(STATIC_PAIRS) > 200


def test_one_key_never_carries_two_different_fallbacks() -> None:
    # Two call sites sharing a key but disagreeing on the text means one of them
    # will render the other's wording as soon as a bundle is loaded.
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for key, fallback in T_CALL_RE.findall(JS):
        if key in seen and seen[key] != fallback:
            clashes.append(f"{key}: {seen[key]!r} vs {fallback!r}")
        seen[key] = fallback
    assert not clashes, clashes


def test_every_key_used_by_the_page_exists_in_both_bundles() -> None:
    missing_zh = sorted(key for key in STATIC_PAIRS if key not in ZH_KEYS)
    missing_en = sorted(key for key in STATIC_PAIRS if key not in EN_KEYS)
    assert not missing_zh, f"zh-CN.json is missing: {missing_zh}"
    assert not missing_en, f"en-US.json is missing: {missing_en}"


def test_zh_bundle_matches_the_call_site_fallbacks() -> None:
    # The fallback is what a user sees when the bundle fails to load; if the two
    # disagree the wording changes depending on load order.
    drift = sorted(
        f"{key}: json={ZH_KEYS[key]!r} code={fallback!r}"
        for key, fallback in STATIC_PAIRS.items()
        if key in ZH_KEYS and ZH_KEYS[key] != fallback
    )
    assert not drift, drift


def test_bundles_have_the_same_keys_and_no_orphans() -> None:
    assert sorted(ZH_KEYS) == sorted(EN_KEYS)

    orphans = sorted(
        key
        for key in ZH_KEYS
        if key not in STATIC_PAIRS and not key.startswith(DYNAMIC_PREFIXES)
    )
    assert not orphans, f"translated keys nothing reads: {orphans}"


CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def test_english_bundle_is_translated_not_copied() -> None:
    # Deliberately identical strings (units, "PROCESS FOOTPRINT", "Pss") are
    # fine; the failure mode worth catching is Chinese prose left untranslated.
    untranslated = sorted(
        key
        for key, value in ZH_KEYS.items()
        if CJK_RE.search(str(value)) and EN_KEYS.get(key) == value
    )
    assert not untranslated, f"en-US.json still holds Chinese text: {untranslated}"

    # And the reverse: an English value that still carries Chinese characters.
    leaked = sorted(key for key, value in EN_KEYS.items() if CJK_RE.search(str(value)))
    assert not leaked, f"en-US.json contains Chinese characters: {leaked}"

    for bundle in (ZH, EN):
        assert bundle["metadata"]["display_name"]
        assert bundle["metadata"]["description"]
