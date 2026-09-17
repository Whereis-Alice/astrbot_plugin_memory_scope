"""Structural regressions for the shared observer/dashboard application."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "pages/memory"
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
CSS = (ASSETS / "style.css").read_text(encoding="utf-8")


def test_both_entrypoints_share_the_entire_application():
    standalone = (ASSETS / "observer.html").read_text(encoding="utf-8")
    assert standalone.replace('data-mode="standalone"', 'data-mode="embedded"') == HTML
    assert HTML.count("<main ") == 1
    assert "observer.js" not in HTML and "observer.css" not in HTML


def test_hidden_drawer_cannot_cover_the_application():
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", CSS)
    assert re.search(r'<div id="drawer"[^>]* hidden', HTML)
    assert 'aria-modal="true"' in HTML


def test_all_relative_module_assets_exist():
    for path in ASSETS.glob("*.js"):
        for name in re.findall(
            r'from\s+[\'"]\./([^\'"]+)', path.read_text(encoding="utf-8")
        ):
            assert (ASSETS / name).is_file(), (path, name)
    for name in re.findall(r'(?:href|src)="\./([^"?]+)', HTML):
        assert (ASSETS / name).is_file()


def test_translation_bundles_match_shared_application():
    text = (ASSETS / "locale.js").read_text(encoding="utf-8")
    messages = json.JSONDecoder().raw_decode(text.split(" = ", 1)[1])[0]
    zh = json.loads(
        (ROOT / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8")
    )
    en = json.loads(
        (ROOT / ".astrbot-plugin/i18n/en-US.json").read_text(encoding="utf-8")
    )
    assert zh["pages"]["memory"] == {k: v[0] for k, v in messages.items()}
    assert en["pages"]["memory"] == {k: v[1] for k, v in messages.items()}
    assert all(not re.search(r"[\u4e00-\u9fff]", v[1]) for v in messages.values())
    keys = set(re.findall(r'data-t="([^"]+)"', HTML))
    for name in ("app.js", "chart.js"):
        source = (ASSETS / name).read_text(encoding="utf-8")
        keys.update(re.findall(r'\bt\("([^"\n]+)"\)', source))
        # Includes labels passed indirectly through navigation and guide models.
        keys.update(
            s
            for s in re.findall(r'"([^"\n]*)"', source)
            if re.search(r"[\u4e00-\u9fff]", s) and not re.search("[<>$]", s)
        )
    assert len(keys) > 200
    assert not keys - set(messages), sorted(keys - set(messages))


def test_theme_tokens_and_motion_accessibility():
    assert 'data-scope-theme="midnight"' in CSS and 'data-scope-theme="plum"' in CSS
    assert "prefers-reduced-motion" in CSS
    assert ":focus-visible" in CSS
