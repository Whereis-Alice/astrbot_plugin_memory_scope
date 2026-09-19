"""Observer routes inherit AstrBot dashboard authentication; credentials stay server-side."""

from __future__ import annotations

from .web_api import _body, _query, error_response, ok


class ObserverApi:
    def __init__(self, client):
        self.client = client

    def register(self, context, plugin_id):
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            return

        async def status():
            return ok(
                {
                    "enabled": self.client.enabled,
                    "configured": self.client.configured,
                    "error": self.client.config_error,
                }
            )

        register(
            f"/{plugin_id}/observer_status", status, ["GET"], "MemoryScope 数据源状态"
        )
        for name in (
            "overview",
            "runs",
            "run",
            "trend",
            "compare",
            "experiments",
            "diagnostics",
            "activities",
            "growth",
        ):

            def make_handler(endpoint):
                async def handler():
                    try:
                        query = await _query()
                        allowed = {
                            k: v
                            for k, v in query.items()
                            if k
                            in {
                                "id",
                                "A",
                                "B",
                                "seconds",
                                "samples",
                                "since",
                                "until",
                                "before",
                                "plugin",
                                "category",
                                "limit",
                            }
                        }
                        return ok(await self.client.get(endpoint, allowed))
                    except ValueError as exc:
                        return error_response(str(exc), status_code=503)

                return handler

            register(
                f"/{plugin_id}/observer_{name}",
                make_handler(name),
                ["GET"],
                f"MemoryScope 独立观察 {name}",
            )
        for name, endpoint in (("start", "experiments"), ("cancel", "cancel")):

            def make_post(target):
                async def handler():
                    try:
                        return ok(await self.client.post(target, await _body()))
                    except ValueError as exc:
                        return error_response(str(exc), status_code=400)

                return handler

            register(
                f"/{plugin_id}/observer_{name}",
                make_post(endpoint),
                ["POST"],
                "MemoryScope 受控重启实验",
            )
