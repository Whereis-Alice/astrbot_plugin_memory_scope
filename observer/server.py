"""Loopback HTTP API and read-only standalone report UI. No web dependencies."""

from __future__ import annotations

import hmac
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .activity import CATEGORIES, number
from .analysis import compare
from .experiments import Experiments

MAX_BODY = 256 * 1024


def make_server(observer, assets: Path | None = None):
    experiments = Experiments(observer)
    assets = assets or Path(__file__).resolve().parent.parent / "pages/memory"

    class Handler(BaseHTTPRequestHandler):
        server_version = "MemoryScope/4"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Never log Authorization or query strings.

        def send(self, code: int, data, content_type="application/json; charset=utf-8"):
            body = (
                data
                if isinstance(data, bytes)
                else json.dumps(data, ensure_ascii=False).encode()
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            expected = "Bearer " + observer.config.token
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

        def do_GET(self):
            url = urlsplit(self.path)
            if not url.path.startswith("/api/"):
                mapping = {
                    "/": ("observer.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/chart.js": ("chart.js", "text/javascript; charset=utf-8"),
                    "/model.js": ("model.js", "text/javascript; charset=utf-8"),
                    "/activity-view.js": (
                        "activity-view.js",
                        "text/javascript; charset=utf-8",
                    ),
                    "/activity-filter.js": (
                        "activity-filter.js",
                        "text/javascript; charset=utf-8",
                    ),
                    "/locale.js": ("locale.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }
                if url.path not in mapping:
                    return self.send(404, {"error": "Not found"})
                name, mime = mapping[url.path]
                try:
                    return self.send(200, (assets / name).read_bytes(), mime)
                except OSError:
                    return self.send(404, {"error": "UI assets not installed"})
            if not self.authorized():
                return self.send(401, {"error": "Unauthorized"})
            query = parse_qs(url.query)
            try:
                if url.path == "/api/overview":
                    result = observer.overview()
                elif url.path == "/api/runs":
                    result = observer.store.runs()
                elif url.path == "/api/run":
                    run_id = query.get(
                        "id", [(observer.current_run or {}).get("id", "")]
                    )[0]
                    result = observer.report(
                        run_id, include_samples=query.get("samples", ["1"])[0] != "0"
                    )
                    # Bound chart traffic without discarding peaks (min/max per bucket).
                    points = result.get("samples", [])
                    if len(points) > 1500:
                        step = max(2, len(points) // 750)
                        selected = []
                        for index in range(0, len(points), step):
                            bucket = points[index : index + step]
                            key = lambda s: s.get("memory", {}).get("anon_swap") or 0
                            extrema = {
                                s["ts"]: s
                                for s in (min(bucket, key=key), max(bucket, key=key))
                            }
                            selected.extend(extrema[k] for k in sorted(extrema))
                        result["samples"] = selected
                    result["samples"] = [
                        {k: s[k] for k in ("ts", "memory", "state") if k in s}
                        for s in result.get("samples", [])
                    ]
                elif url.path == "/api/trend":
                    run_id = query.get(
                        "id", [(observer.current_run or {}).get("id", "")]
                    )[0]
                    seconds = int(query.get("seconds", ["3600"])[0])
                    if seconds not in {0, 900, 3600, 21600, 86400}:
                        raise ValueError("Unsupported time range")
                    result = observer.trend(run_id, seconds)
                elif url.path == "/api/compare":
                    groups = {
                        label: [
                            observer.summary(i)
                            for i in query.get(label, [""])[0].split(",")
                            if i
                        ]
                        for label in ("A", "B")
                    }
                    if any(len(v) > 10 for v in groups.values()):
                        raise ValueError("At most 10 runs per group")
                    result = compare(groups)
                elif url.path == "/api/experiments":
                    result = observer.store.jobs()
                elif url.path in {"/api/activities", "/api/growth"}:
                    run_id = query.get(
                        "id", [(observer.current_run or {}).get("id", "")]
                    )[0]
                    since = float(query.get("since", ["0"])[0])
                    until = float(
                        query.get("until", [str(__import__("time").time())])[0]
                    )
                    if (
                        number(since) is None
                        or number(until) is None
                        or since < 0
                        or until < since
                    ):
                        raise ValueError("Invalid time interval")
                    if url.path.endswith("growth"):
                        result = observer.growth(run_id, since, until)
                    else:
                        category = query.get("category", [""])[0]
                        if category and category not in CATEGORIES:
                            raise ValueError("Invalid category")
                        before = int(query["before"][0]) if "before" in query else None
                        result = observer.activities(
                            run_id,
                            since=since,
                            until=until,
                            before=before,
                            category=category,
                            plugin=query.get("plugin", [""])[0][:120],
                            plugin_filter=query.get("plugin_filter", [None])[0],
                            limit=int(query.get("limit", ["50"])[0]),
                        )
                elif url.path == "/api/diagnostics":
                    run_id = query.get(
                        "id", [(observer.current_run or {}).get("id", "")]
                    )[0]
                    result = observer.diagnostics(run_id)
                else:
                    return self.send(404, {"error": "Not found"})
                self.send(200, {"status": "ok", "data": result})
            except ValueError as exc:
                self.send(400, {"error": str(exc)})
            except Exception:  # noqa: BLE001 - API boundary
                self.send(500, {"error": "Observer request failed"})

        def do_POST(self):
            if not self.authorized():
                return self.send(401, {"error": "Unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.send(413, {"error": "Request size limit"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise TypeError("Expected JSON object")
                path = urlsplit(self.path).path
                if path in {"/api/inventory", "/api/activities"}:
                    event = dict(
                        payload,
                        kind="inventory"
                        if path.endswith("inventory")
                        else "activity_batch",
                    )
                    result = {
                        "accepted": observer.accept_event(
                            {"token": observer.config.token, "event": event}
                        )
                    }
                elif path == "/api/diagnostics":
                    event = dict(payload, kind="diagnostic")
                    result = {
                        "accepted": observer.accept_event(
                            {"token": observer.config.token, "event": event}
                        )
                    }
                elif path == "/api/experiments":
                    result = experiments.create(payload)
                elif path == "/api/cancel":
                    result = experiments.cancel()
                else:
                    return self.send(404, {"error": "Not found"})
                self.send(200, {"status": "ok", "data": result})
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:  # noqa: BLE001 - API boundary
                self.send(500, {"error": "Observer request failed"})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 16
        address_family = (
            socket.AF_INET6 if observer.config.host == "::1" else socket.AF_INET
        )

        def __init__(self, *args):
            self.slots = __import__("threading").BoundedSemaphore(8)
            super().__init__(*args)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    server = Server((observer.config.host, observer.config.port), Handler)
    server.experiments = experiments
    return server
