# gma3-claude-bridge

Lets Claude (or any script) run Lua in a live grandMA3 onPC show, through a folder
on disk.

A client drops a Lua snippet into `claude_mailbox/requests/`. The ClaudeMailbox
plugin, running inside MA, picks it up, runs it and writes the result to
`claude_mailbox/responses/`. No network, no ports.

## What's here

| File | What it is |
| --- | --- |
| `ClaudeMailbox.lua` / `.xml` | Plugin for grandMA3 2.3 (Lua 5.4). Mailbox: `claude_mailbox/` |
| `ClaudeMailbox25.lua` / `.xml` | Plugin for grandMA3 2.5 (Lua 5.5). Mailbox: `claude_mailbox/v25/` |
| `claude_mailbox/ma3.py` | Command-line client: `status`, `run` |
| `claude_mailbox/ma3_mcp.py` | MCP server for Claude Code: `run_lua`, `list_pool`, `dump_object`, `move_object` |
| `claude_mailbox/dashboard/` | Read-only local web dashboard, http://127.0.0.1:8733 |
| `.mcp.json` | Registers the MCP server for both MA versions |
| `ReaperBridge.xml` | Separate ReaperBridge plugin build |

Each MA version has its own mailbox, so 2.3 and 2.5 can run side by side.

## Safety

- Start the plugin with `"confirm"` to approve every request on the console.
  Only the console can turn that off.
- `move_object` is a dry run unless told otherwise, and uses MA's `Move`, so `Oops` undoes it.
- The dashboard has no route that writes to MA or accepts Lua.

See [SETUP.md](SETUP.md) to get it running.
