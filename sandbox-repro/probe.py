# Drive Hermes's REAL tool-dispatch path with the ACP plugin loaded, no LLM involved.
# Prints which hooks the plugin manager holds, then runs one terminal command through
# model_tools.handle_function_call, which is the same path the agent loop uses.
# Debug logging goes to stderr so a swallowed "pre_tool_call hook error" becomes visible.
import logging
import sys

logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s %(message)s", stream=sys.stderr)

from hermes_cli import plugins  # noqa: E402

mgr = plugins.get_plugin_manager()
mgr.discover_and_load(force=True)

names = None
for attr in ("plugins", "_plugins", "loaded", "_loaded"):
    v = getattr(mgr, attr, None)
    if v:
        names = [getattr(p, "name", None) or getattr(getattr(p, "manifest", None), "name", None) or str(p) for p in (v.values() if isinstance(v, dict) else v)]
        break
print("loaded plugins:", names)

hooks = getattr(mgr, "_hooks", None) or getattr(mgr, "hooks", None)
if isinstance(hooks, dict):
    for name in ("pre_tool_call", "post_tool_call"):
        print(f"registered {name}:", hooks.get(name))
else:
    print("hook registry attribute not found on manager; attrs:", [a for a in dir(mgr) if "hook" in a.lower()])

from model_tools import handle_function_call  # noqa: E402

res = handle_function_call("terminal", {"command": "echo test_canary"}, task_id="sandbox-task")
print("dispatch result:", str(res)[:300])
