"""topic-router plugin entry point. Config: plugins.entries.topic-router.settings (see plugin.yaml)."""
from __future__ import annotations

from .decider import TASK, build_decider
from .router import Router


def register(ctx):
    ctx.register_auxiliary_task(
        TASK, display_name="Topic router",
        description="Decides whether a new chat message continues the current session.")
    spec = ctx.get_config("decider", {}) or {}
    router = Router(build_decider(spec, lambda: ctx.llm), ctx.get_config("channels", {}) or {},
                    decide_timeout=float(spec.get("timeout_seconds") or 120) + 5,
                    spawn=lambda coro: ctx.spawn_task(coro, name="topic-router:prompt"))
    ctx.register_hook("pre_gateway_dispatch", router.on_dispatch)
    ctx.register_hook("on_session_reset", router.on_reset)
    return router
