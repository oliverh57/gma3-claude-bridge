# Setup

Needs macOS, grandMA3 onPC 2.3 or 2.5, and Python 3 (standard library only).

## 1. Put the repo in the MA plugins folder

The plugins expect this exact path:

```bash
git clone https://github.com/oliverh57/gma3-claude-bridge.git ~/MALightingTechnology/gma3_library/datapools/plugins
```

If that folder already exists, clone somewhere else and copy the files in.

## 2. Create the mailbox folders

```bash
cd ~/MALightingTechnology/gma3_library/datapools/plugins/claude_mailbox && mkdir -p requests responses archive v25/requests v25/responses v25/archive
```

## 3. Import and start the plugin in MA

In the Plugins pool, import `ClaudeMailbox` (2.3) or `ClaudeMailbox25` (2.5). Then
on the command line:

```
Plugin "ClaudeMailbox" "start"
```

| Command | Effect |
| --- | --- |
| `Plugin "ClaudeMailbox"` | start, or stop if running |
| `Plugin "ClaudeMailbox" "start"` | start with no confirmation |
| `Plugin "ClaudeMailbox" "confirm"` | start, asking on the console before each request |
| `Plugin "ClaudeMailbox" "stop"` | stop |

For 2.5, use `ClaudeMailbox25` in place of `ClaudeMailbox`.

## 4. Check it from a terminal

```bash
cd ~/MALightingTechnology/gma3_library/datapools/plugins/claude_mailbox && python3 ma3.py status
```

```bash
python3 ma3.py run -e 'return 1+1'
```

For 2.5, put `MA3_MAILBOX=v25` before each command.

## 5. Claude Code

Open Claude Code in the plugins folder. `.mcp.json` registers `grandma3` (2.3) and
`grandma3-25` (2.5). If your home folder isn't `/Users/oliverhynds`, change the
script paths in `.mcp.json` to match. Change `/opt/homebrew/bin/python3` too if
your Python is somewhere else.

## Dashboard (optional)

```bash
python3 claude_mailbox/dashboard/server.py
```

Then open http://127.0.0.1:8733. Add `--no-console` to read the mailbox files
only and send nothing to MA.
