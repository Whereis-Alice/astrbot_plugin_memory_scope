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
                "current": 400 * 1048576,
            },
            "host": {"available": 2000 * 1048576},
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
        page = browser.new_page(
            viewport={"width": width, "height": height}, device_scale_factor=1
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.get_by_label("后端访问凭据").fill(token)
        page.get_by_role("button", name="连接", exact=True).click()
        page.get_by_text("服务物理记账", exact=True).wait_for()
        assert page.locator("svg.ob-chart").count() == 1
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth + 1"
        )
        directory = os.environ.get("MEMORYSCOPE_QA_DIR")
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(Path(directory) / f"observer-{width}.png"), full_page=True
            )
        page.get_by_role("button", name="启动报告", exact=True).click()
        page.get_by_role("heading", name="逐插件加载阶段").wait_for()
        assert (
            page.get_by_role("cell", name="astrbot_plugin_example", exact=True).count()
            >= 1
        )
        page.get_by_role("button", name="对照实验", exact=True).click()
        page.get_by_role("button", name="创建实验", exact=True).wait_for()
        assert page.get_by_role("button", name="创建实验", exact=True).is_disabled()
        page.get_by_role("button", name="测量说明", exact=True).click()
        page.get_by_role("heading", name="三种证据，各回答一个问题").wait_for()
        assert not errors
        browser.close()
    assert len(observer.store.rows("samples", "ui-fixture:1:123")) == count


def test_embedded_dashboard_uses_bridge_without_exposing_credentials(preview):
    url, token, _observer = preview
    assets = Path(__file__).resolve().parents[1] / "pages/memory"
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_static(route):
            name = route.request.url.rsplit("/", 1)[-1]
            if name not in {
                "index.html",
                "app.js",
                "style.css",
                "observer.js",
                "observer.css",
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
            route.fulfill(
                status=200, body=(assets / name).read_bytes(), content_type=mime
            )

        page.route(url + "/plugin/**", route_static)
        page.add_init_script("""
            window.AstrBotPluginPage = {
                ready: async () => {}, t: (_key, fallback) => fallback,
                getContext: () => ({isDark: true}), onContext: () => {},
                apiGet: async (name, params) => {
                    if (!name.startsWith('observer_')) throw new Error('Unexpected local scan');
                    return window.testObserverBridge(name, params);
                },
            };
        """)

        def read_api(name, params):
            import json
            from urllib.parse import urlencode
            from urllib.request import Request, urlopen

            endpoint = name.removeprefix("observer_")
            req = Request(
                url + "/api/" + endpoint + "?" + urlencode(params or {}),
                headers={"Authorization": "Bearer " + token},
            )
            with urlopen(req, timeout=10) as response:
                return json.load(response)

        page.expose_function("testObserverBridge", read_api)
        page.goto(url + "/plugin/index.html")
        page.get_by_text("服务物理记账", exact=True).wait_for()
        assert (
            page.locator("#panel-observer").get_attribute("class") == "panel is-active"
        )
        assert page.locator("#ob-auth").is_hidden()
        assert token not in page.content()
        assert not errors
        browser.close()
