# Third probe: the exact entry points Hermes 0.19's executor uses for a terminal call.
#   pre : hermes_cli.plugins.resolve_pre_tool_block  (tool_executor.py ~1118)
#   post: model_tools._emit_post_tool_call_hook       (tool_executor.py ~160 via _emit_terminal_post_tool_call)
# Requires the plugin to be ENABLED (run.sh does `hermes plugins enable acp` first).
import logging
import sys

logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s %(message)s", stream=sys.stderr)

from hermes_cli import plugins  # noqa: E402

mgr = plugins.get_plugin_manager()
mgr.discover_and_load(force=True)
h = mgr._hooks
print("registered pre_tool_call:", [f"{c.__module__}.{c.__name__}" for c in h.get("pre_tool_call", [])])
print("registered post_tool_call:", [f"{c.__module__}.{c.__name__}" for c in h.get("post_tool_call", [])])

args = {"command": "echo test_canary"}
print("resolve_pre_tool_block ->", plugins.resolve_pre_tool_block(
    "terminal", args, task_id="t3", session_id="s3", tool_call_id="c3", turn_id="u3", api_request_id="a3"))

import model_tools  # noqa: E402

emit = getattr(model_tools, "_emit_post_tool_call_hook", None)
if emit is None:
    print("_emit_post_tool_call_hook not present in this Hermes version")
else:
    try:
        emit("terminal", args, "test_canary", task_id="t3", session_id="s3", turn_id="u3",
             tool_call_id="c3", duration_ms=5, status="ok")
        print("_emit_post_tool_call_hook -> called")
    except TypeError as e:
        print("_emit_post_tool_call_hook signature differs:", e)
        import inspect
        print(inspect.signature(emit))
