# hermes-acp

ACP governance plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Routes every tool call through the [Agentic Control Plane](https://agenticcontrolplane.com) so you get:

- **Audit** — every tool call (terminal, file, web, browser, custom skills) is logged with tenant + session attribution.
- **Veto** — server-side policy can deny or require approval on individual tool calls before they execute.

Companion to the [Claude Code ACP plugin](https://github.com/davidcrowe/claude-code-acp-plugin). Same backend contract, same dashboard, same policies — just wired into Hermes's Python plugin system instead of Claude Code's shell hooks.

## Install

```bash
pip install hermes-acp
hermes plugins enable acp
hermes-acp login
```

`hermes-acp login` opens the dashboard, exchanges your one-time auth token for a workspace API key, and writes it to `~/.acp/credentials`.

## Configure

For non-interactive setups (CI, devcontainers), skip the `login` step and provide the key directly:

```bash
export ACP_BEARER_TOKEN="gsk_yourslug_..."
# or
mkdir -p ~/.acp && echo "gsk_yourslug_..." > ~/.acp/credentials
```

The env var wins over the file.

## CLI

```bash
hermes-acp login       # browser-based authentication + workspace provisioning
hermes-acp status      # check creds + gateway reachability
hermes-acp logout      # remove ~/.acp/credentials
```

Optional — point at a non-default backend:

```bash
export ACP_API_BASE="https://api.agenticcontrolplane.com"  # default
```

## How it works

The plugin registers two Hermes hooks:

| Hook            | Behavior                                                      |
|-----------------|---------------------------------------------------------------|
| `pre_tool_call` | POSTs to `/govern/tool-use`. Server returns `allow` / `deny` / `ask`. `deny` blocks with a system message; `ask` escalates to Hermes's native approval prompt; `allow` passes through. |
| `post_tool_call`| POSTs to `/govern/tool-output` for observation. Cannot block (Hermes limitation), but server-side audit, redaction logging, and DLP scanning all apply. |

### Fail-open

Network errors, timeouts (>4s), or malformed responses **fail open** — the tool call proceeds and a warning is written to stderr. ACP outages should never block your work. Server-side per-tenant `failMode: closed` can flip this in a future release.

### "Ask" semantic

An ACP `ask` decision escalates to **Hermes's native approval gate** — the same inline `[o]nce / [s]ession / [a]lways / [d]eny` prompt Hermes uses for dangerous shell commands. An `[a]lways` answer is scoped to the tool via the plugin's `rule_key` (`acp:<tool>`), so a standing approval never widens beyond the tool the human actually reviewed. (Versions ≤0.1.0 wrongly claimed Hermes had no approval surface and hard-blocked with a dashboard detour — upgrade.)

### Client identity

Sends `X-GS-Client: hermes-plugin/<version>` so the dashboard, policy router, and audit log can distinguish Hermes traffic from Claude Code / Cursor / Codex / etc.

## Troubleshooting

**No audit events appearing.** Check that `ACP_BEARER_TOKEN` is set in the shell that launched `hermes`, not just your `.zshrc` after the fact. Hermes inherits the env at process start.

**Every tool call blocked.** Look at stderr. A `[ACP] gateway unreachable` warning means network failure (fail-open kicked in but something else blocked you — maybe a policy from another hook). A `[ACP] Denied by policy: …` means the server returned `deny`; check the policy in the ACP dashboard.

**Hooks not firing at all.** Confirm the plugin is enabled: `hermes plugins list`. If `acp` isn't in the enabled list, run `hermes plugins enable acp`.

## License

MIT
