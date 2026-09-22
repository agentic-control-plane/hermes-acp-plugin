# Second probe: inspect the Hermes hook registry and call both plugin hooks directly.
# Answers: is the acp plugin's pre_tool_call actually registered, does calling it
# produce a request, and does the installed Hermes dispatch path fire hooks at all.
import glob
import os
import subprocess
import sys

from hermes_cli import plugins

site = os.path.dirname(os.path.dirname(plugins.__file__))
print("hermes site-packages:", site)
for rel in ("model_tools.py", "agent/tool_executor.py"):
    p = os.path.join(site, rel)
    if os.path.exists(p):
        out = subprocess.run(["grep", "-n", "pre_tool_call\\|post_tool_call", p], capture_output=True, text=True).stdout
        print(f"--- hook call sites in {rel}:\n{out.strip() or '(none)'}")
    else:
        print(f"--- {rel}: not present in this Hermes version")

mgr = plugins.get_plugin_manager()
mgr.discover_and_load(force=True)
h = mgr._hooks
print("--- hook names with callbacks:", {k: len(v) for k, v in h.items() if v})
for name in ("pre_tool_call", "post_tool_call"):
    print(name, "->", [f"{getattr(c, '__module__', '?')}.{getattr(c, '__name__', '?')}" for c in h.get(name, [])])

import acp_hermes  # noqa: E402

print("--- plugin config: token resolved =", bool(acp_hermes._resolve_token()), "| api base =", acp_hermes._api_base())

print("--- invoke pre_tool_call directly:", plugins.invoke_hook(
    "pre_tool_call", tool_name="terminal", args={"command": "echo test_canary"}, task_id="t1"))
print("--- invoke post_tool_call directly:", plugins.invoke_hook(
    "post_tool_call", tool_name="terminal", args={"command": "echo test_canary"}, result="test_canary", task_id="t1"))
print("--- call plugin function directly:", acp_hermes._pre_tool_call("terminal", {"command": "echo test_canary"}, task_id="t1"))
