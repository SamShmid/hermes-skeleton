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
                    spawn=lambda coro: ctx.spawn_task(coro, name="topic-router:prompt"),
                    confirm_all=bool(ctx.get_config("confirm_all", False)),
                    act_confirm=bool(ctx.get_config("act_confirm", False)),
                    label_log=ctx.get_config("label_log", None),
                    threadify=ctx.get_config("threadify_channels", {}) or {},
                    owner_label=str(ctx.get_config("owner_label", "Owner") or "Owner"),
                    threadify_state=ctx.get_config("threadify_state", "~/.hermes/state/topic_router_threadify.json"),
                    answer_timeout=float(ctx.get_config("answer_timeout_seconds", 600) or 600),
                    held_state=ctx.get_config("held_state", "~/.hermes/state/topic_router_held.json"),
                    reply=ctx.get_config("reply_channels", {}) or {},
                    reply_state=ctx.get_config("reply_state", "~/.hermes/state/topic_router_replies.json"))
    router.announce_leftovers()
    ctx.register_hook("pre_gateway_dispatch", router.on_dispatch)
    ctx.register_hook("on_session_reset", router.on_reset)
    return router
