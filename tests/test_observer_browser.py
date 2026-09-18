"""Real-browser smoke tests against an isolated observer with synthetic history."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from observer.config import Config
from observer.runtime import Observer
from observer.server import make_server


@pytest.fixture
def preview(tmp_path):
    config = Config(
        token="memoryscope-ui-test-only-0000000000", state_dir=str(tmp_path), port=0
    )
    observer = Observer(config)
    now = time.time()
    run = {
        "id": "ui-fixture:1:123",
        "pid": 1,
        "start_ticks": 123,
        "started_at": now - 180,
        "ready_at": now - 120,
        "fingerprint": "ui-fixture",
        "probe_enabled": True,
        "inventory": [
            {
                "name": "example",
                "display_name": "示例图片插件",
                "root_dir_name": "astrbot_plugin_example",
                "activated": True,
                "reserved": False,
            }
        ],
    }
    observer.store.save_run(run)
    for i in range(37):
        sample = {
            "ts": now - 180 + i * 5,
            "state": "running",
            "memory": {
                "anon_swap": (300 + i) * 1048576,
                "anon": (300 + i) * 1048576,
                "swap": 0,
                "current": (400 + i % 8 * 2 + (15 if i == 22 else 0)) * 1048576,
            },
            "host": {"available": 2000 * 1048576, "total": 4000 * 1048576},
            "processes": [
                {
                    "pid": 1,
                    "start_ticks": 123,
                    "name": "python",
                    "rss": 350 * 1048576,
                    "swap": 0,
                    "pss": 340 * 1048576,
                    "detailed_at": now - 20,
                }
            ],
        }
        observer.store.append("samples", run["id"], sample)
    observer.current_run, observer.latest = run, sample
    observer.store.append(
        "events",
        run["id"],
        {
            "ts": now - 170,
            "monotonic_ns": 1000000000,
            "seq": 1,
            "kind": "phase_start",
            "plugin": "astrbot_plugin_example",
            "phase": "import",
            "counters": {"VmRSS": 10000000, "VmSwap": 0},
        },
    )
    observer.store.append(
        "events",
        run["id"],
        {
            "ts": now - 169,
            "monotonic_ns": 2000000000,
            "seq": 2,
            "span": 1,
            "kind": "phase_end",
            "plugin": "astrbot_plugin_example",
            "phase": "import",
            "counters": {"VmRSS": 20000000, "VmSwap": 0},
            "new_packages": ["numpy"],
        },
    )
    observer.store.append(
        "events",
        run["id"],
        {"ts": now - 168, "seq": 3, "kind": "probe_finished", "overhead_ms": 2},
    )
    server = make_server(observer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", config.token, observer
    server.shutdown()
    server.server_close()
    observer.close()


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_report_navigation_and_mobile_layout(preview, width, height):
    url, token, observer = preview
    count = len(observer.store.rows("samples", "ui-fixture:1:123"))
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.get_by_label("后端访问凭据").fill(token)
        page.get_by_role("button", name="连接", exact=True).click()
        page.locator("#trend svg").wait_for()
        assert page.locator("#auth").is_hidden()
        assert page.locator(".chart-line").count() >= 1
        page.locator("#trend svg").focus()
        page.keyboard.press("ArrowRight")
        assert page.locator(".chart-tip").is_visible()
        assert "MiB" in page.locator(".chart-tip").inner_text()
        if width > 800:
            box = page.locator("#trend svg").bounding_box()
            page.mouse.move(box["x"] + 100, box["y"] + 120)
            page.mouse.down()
            page.mouse.move(box["x"] + 300, box["y"] + 120, steps=5)
            page.mouse.up()
            assert page.locator("#chart-reset").is_visible()
            page.locator("#chart-reset").click()
        for theme in ("paper", "midnight", "plum"):
            page.locator("#theme").click()
            page.locator(f'[data-theme-choice="{theme}"]').click()
            snapshot(page, f"overview-{theme}-{width}")
        page.locator('[data-go="plugins"]').first.click()
        page.get_by_role("button", name="示例图片插件", exact=True).click()
        assert page.locator("#drawer").is_visible()
        assert "9.54 MiB" in page.locator("#detail").inner_text()
        page.keyboard.press("Escape")
        assert page.locator("#drawer").is_hidden()
        assert not page.locator("#workspace").evaluate("el=>el.inert")
        page.get_by_label("搜索插件", exact=True).fill("missing")
        page.get_by_role("heading", name="没有匹配的插件").wait_for()
        page.get_by_label("搜索插件", exact=True).fill("example")
        page.locator("#refresh").click()
        assert page.get_by_label("搜索插件", exact=True).input_value() == "example"
        page.locator('[data-go="startup"]').click()
        page.get_by_role("heading", name="逐插件加载阶段").wait_for()
        assert (
            page.locator("#phase-table")
            .get_by_role("button", name="astrbot_plugin_example")
            .count()
            == 1
        )
        snapshot(page, f"startup-{width}")
        page.locator('[data-go="diagnostics"]').click()
        page.get_by_role("heading", name="尚未运行诊断").wait_for()
        assert page.get_by_role("button", name="运行一次").first.is_disabled()
        snapshot(page, f"diagnostics-{width}")
        page.locator('[data-go="experiments"]').click()
        assert page.get_by_role("button", name="创建实验", exact=True).is_disabled()
        snapshot(page, f"experiments-{width}")
        page.locator('[data-go="guide"]').first.click()
        page.get_by_role("heading", name="先看服务整体").wait_for()
        snapshot(page, f"guide-{width}")
        assert not errors, errors
        browser.close()
    assert len(observer.store.rows("samples", "ui-fixture:1:123")) == count


def snapshot(page, name):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1"
    )
    directory = os.environ.get("MEMORYSCOPE_QA_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / (name + ".png")), full_page=True)


def mount_bridge(page, url, token, status=None, extra=None):
    assets = Path(__file__).resolve().parents[1] / "pages/memory"
    calls = []

    def route_static(route):
        name = route.request.url.rsplit("/", 1)[-1]
        if name not in {
            "index.html",
            "app.js",
            "model.js",
            "chart.js",
            "locale.js",
            "style.css",
        }:
            route.abort()
            return
        mime = (
            "text/html"
            if name.endswith(".html")
            else "text/css"
            if name.endswith(".css")
            else "text/javascript"
        )
        route.fulfill(status=200, body=(assets / name).read_bytes(), content_type=mime)

    page.route(url + "/plugin/**", route_static)
    page.add_init_script("""
        window.AstrBotPluginPage={ready:async()=>{},getContext:()=>({isDark:true}),
          apiGet:async(name,params)=>window.testBridge(name,params,"GET"),
          apiPost:async(name,params)=>window.testBridge(name,params,"POST")};
    """)

    def read_api(name, params, method):
        import json
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        calls.append((name, params, method))
        if name == "preferences":
            return {}
        if name == "observer_status":
            return {
                "status": "ok",
                "data": status or {"enabled": True, "configured": True},
            }
        if extra and name in extra:
            return extra[name]
        if name == "plugins":
            assert params == {"sample": 0, "census": 0, "audit": 0, "deep": 0}
            return {
                "data": {
                    "status": "ok",
                    "data": {"plugins": [], "generated_at": time.time()},
                }
            }
        if name == "alerts":
            return {"alerts": []}
        assert name.startswith("observer_")
        req = Request(
            url
            + "/api/"
            + name.removeprefix("observer_")
            + "?"
            + urlencode(params or {}),
            headers={"Authorization": "Bearer " + token},
        )
        with urlopen(req, timeout=10) as response:
            return {"data": json.load(response), "status": 200}

    page.expose_function("testBridge", read_api)
    return calls


def test_embedded_dashboard_uses_bridge_without_exposing_credentials(preview):
    url, token, _ = preview
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        calls = mount_bridge(page, url, token)
        page.goto(url + "/plugin/index.html#plugins")
        page.get_by_role("button", name="示例图片插件", exact=True).wait_for()
        assert page.locator("#auth").is_hidden()
        assert token not in page.content()
        assert any(name == "plugins" for name, _, _ in calls)
        page.locator('[data-go="diagnostics"]').click()
        page.wait_for_function(
            "document.querySelector('#diagnostic-results').textContent.includes('尚未运行诊断')"
        )
        page.wait_for_timeout(150)
        assert any(name == "plugins" for name, _, _ in calls)
        assert not any(method == "POST" for _, _, method in calls)
        assert not errors, errors
        browser.close()


def test_configuration_error_is_not_silently_replaced_with_local_zeros(preview):
    url, token, _ = preview
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        calls = mount_bridge(
            page,
            url,
            token,
            {
                "enabled": True,
                "configured": False,
                "error": "Cannot read backend configuration",
            },
        )
        page.goto(url + "/plugin/index.html")
        page.get_by_role("heading", name="暂时无法读取数据").wait_for()
        assert (
            "Cannot read backend configuration" in page.locator("#notice").inner_text()
        )
        assert [c[0] for c in calls] == ["preferences", "observer_status"]
        browser.close()


def test_local_mode_and_manual_confirmation_do_not_run_automatic_scans(preview):
    url, token, _ = preview
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        fixture = {
            "overview": {
                "server_time": time.time(),
                "process": {"rss_bytes": 314572800},
            },
            "history": {
                "rss": [[time.time() - 10, 300000000], [time.time(), 314572800]]
            },
            "census": {
                "plugins": [
                    {
                        "name": "fixture",
                        "display_name": "Local fixture",
                        "census_measured": True,
                        "census_bytes": 512,
                    }
                ],
                "generated_at": time.time(),
                "census_meta": {"generated_at": time.time()},
            },
        }
        calls = mount_bridge(
            page, url, token, {"enabled": False, "configured": False}, fixture
        )
        page.goto(url + "/plugin/index.html")
        page.locator("#trend svg").wait_for()
        assert "本地模式" in page.locator("#notice").inner_text()
        page.locator('nav [data-go="diagnostics"]').click()
        page.wait_for_timeout(100)
        page.locator('[data-scan="census"]').click()
        assert page.locator("#confirm").is_visible()
        page.get_by_role("button", name="取消", exact=True).click()
        assert not any(method == "POST" for _, _, method in calls)
        page.locator('[data-scan="census"]').click()
        page.get_by_role("button", name="确认执行", exact=True).click()
        page.get_by_role("cell", name="Local fixture", exact=True).wait_for()
        assert page.get_by_role("cell", name="512 B", exact=True).is_visible()
        assert [name for name, _, method in calls if method == "POST"] == ["census"]
        browser.close()


def test_backend_failure_preserves_data_with_error_and_english_navigation(preview):
    url, token, _ = preview
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        page.get_by_label("后端访问凭据").fill(token)
        page.get_by_role("button", name="连接", exact=True).click()
        page.locator("#trend svg").wait_for()
        value = page.locator(".metric-value").first.inner_text()
        page.route(
            url + "/api/overview*",
            lambda route: route.fulfill(status=503, json={"error": "Observer offline"}),
        )
        page.locator("#refresh").click()
        page.wait_for_function(
            "document.querySelector('#notice').textContent.includes('Observer offline')"
        )
        assert page.locator(".metric-value").first.inner_text() == value
        page.locator("#locale").select_option("en-US")
        page.get_by_role("heading", name="Overview", exact=True).wait_for()
        page.get_by_role("button", name="Plugins", exact=True).click()
        assert page.locator("#plugin-count").inner_text() == "1 / 1"
        page.get_by_role("button", name="Guide", exact=True).click()
        page.get_by_role("heading", name="Start with the whole service").wait_for()
        browser.close()


def test_view_models_keep_zero_negative_unknown_and_escape_untrusted_names(preview):
    url, _, _ = preview
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        result = page.evaluate("""async () => {
            const m=await import('/model.js');
            const rows=m.pluginRows({run:{id:'new',inventory:[{root_dir_name:'unknown',display_name:'<img onerror=alert(1)>'}]}},{plugins:[{plugin:'zero',phases:[{process_rss_swap_delta:0,duration_ms:0}]},{plugin:'negative',phases:[{process_rss_swap_delta:-10,duration_ms:1}]}]}, {plugins:[{root_dir_name:'unknown',import_bytes:999}]});
            let failed=false;try{m.unwrap({data:{status:'error',message:'bad'}});}catch{failed=true;}
            return {values:Object.fromEntries(rows.map(r=>[r.id,r.delta])),order:m.sortedRows(rows).map(r=>r.id),escaped:m.esc(rows[0].label),failed,plain:m.unwrap({data:{status:'ok',data:{plugins:[1]}},status:200})};
        }""")
        assert result["values"] == {"unknown": None, "zero": 0, "negative": -10}
        assert result["order"] == ["zero", "negative", "unknown"]
        assert "<img" not in result["escaped"]
        assert result["failed"] and result["plain"] == {"plugins": [1]}
        browser.close()


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844), (320, 740)])
def test_theme_menu_keyboard_readability_and_persistence(preview, width, height):
    """The theme control stays readable and usable independently of OS select colours."""
    import re

    url, _, _ = preview
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url)
        trigger = page.locator("#theme")
        trigger.focus()
        page.keyboard.press("ArrowDown")
        menu = page.get_by_role("menu")
        assert menu.is_visible()
        assert page.locator('[data-theme-choice="paper"]').evaluate(
            "e=>e===document.activeElement"
        )
        page.keyboard.press("End")
        page.keyboard.press("Enter")
        assert menu.is_hidden()
        assert trigger.evaluate("e=>e===document.activeElement")
        assert page.locator("html").get_attribute("data-scope-theme") == "plum"
        page.reload()
        assert page.locator("html").get_attribute("data-scope-theme") == "plum"
        for theme in ("paper", "midnight", "plum"):
            trigger.click()
            page.locator(f'[data-theme-choice="{theme}"]').click()
            trigger.click()
            assert page.get_by_role("menuitemradio", checked=True).count() == 1
            bounds = menu.bounding_box()
            assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= width
            assert bounds["y"] + bounds["height"] <= height
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            # Check text on both ends of card gradients and all semantic badges.
            colours = page.evaluate("""() => {
                const el = document.createElement('span'); document.body.append(el);
                const result = {};
                for (const key of ['ink','body','muted','surface','soft','accent','accent-soft',
                  'data','data-soft','secondary','secondary-soft','violet','violet-soft',
                  'good','good-soft','warn','warn-soft','bad','bad-soft','on-accent']) {
                    el.style.color = `var(--${key})`; result[key] = getComputedStyle(el).color;
                }
                el.remove(); return result;
            }""")

            def luminance(key, colours=colours):
                rgb = [int(v) / 255 for v in re.findall(r"[0-9]+", colours[key])[:3]]
                linear = [
                    v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                    for v in rgb
                ]
                return sum(v * w for v, w in zip(linear, [0.2126, 0.7152, 0.0722]))

            pairs = [
                (fg, bg)
                for fg in ("ink", "body", "muted")
                for bg in ("surface", "soft")
            ]
            pairs += [
                (fg, bg)
                for fg in (
                    "accent",
                    "data",
                    "secondary",
                    "violet",
                    "good",
                    "warn",
                    "bad",
                )
                for bg in ("surface", fg + "-soft")
            ]
            pairs += [("on-accent", "accent")]
            for fg, bg in pairs:
                light, dark = sorted((luminance(fg), luminance(bg)), reverse=True)
                assert (light + 0.05) / (dark + 0.05) >= 4.5, (theme, fg, bg)
            # Escape, Tab, and an outside click all dismiss the menu without a trap.
            page.keyboard.press("Escape")
            assert menu.is_hidden()
            assert trigger.evaluate("e=>e===document.activeElement")
            trigger.click()
            page.keyboard.press("Tab")
            assert menu.is_hidden()
            assert not page.locator("#theme-picker").evaluate(
                "e=>e.contains(document.activeElement)"
            )
            trigger.click()
            page.locator(".brand").click()
            assert menu.is_hidden()
        page.locator("#locale").select_option("en-US")
        trigger.click()
        assert "Afterglow" in menu.inner_text()
        assert not re.search(r"[\u4e00-\u9fff]", menu.inner_text())
        browser.close()
