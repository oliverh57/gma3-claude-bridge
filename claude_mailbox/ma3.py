#!/usr/bin/env python3
"""Client for the ClaudeMailbox grandMA3 plugin.

  ma3.py status                 is the plugin alive, and what did its self-test find
  ma3.py run -e 'return 1+1'    run a snippet
  ma3.py run file.lua           run a file
  ma3.py run -                  run stdin

Prints the response JSON. Exit code 0 if the request ran cleanly, 1 if it failed
or was refused, 2 if no response arrived.

Talks to the 2.3 plugin by default. Set MA3_MAILBOX=v25 for the 2.5 plugin.
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent / os.environ.get("MA3_MAILBOX", "")
PLUGIN = "ClaudeMailbox25" if ROOT.name == "v25" else "ClaudeMailbox"
REQUESTS = ROOT / "requests"
RESPONSES = ROOT / "responses"
HEARTBEAT_STALE_SECONDS = 15


def heartbeat_age():
    """Seconds since the plugin's last heartbeat, or None if it has never written one."""
    try:
        _, stamp = (ROOT / "heartbeat").read_text().split()
    except (OSError, ValueError):
        return None
    return time.time() - int(stamp)


class MailboxError(Exception):
    """The request got no answer. The message says whether it could still be running."""


def is_alive():
    age = heartbeat_age()
    return age is not None and age < HEARTBEAT_STALE_SECONDS and (ROOT / "owner").exists()


def execute(code, timeout=15):
    """Run Lua in MA and return the response dict. Raises MailboxError if none arrives."""
    if not is_alive():
        age = heartbeat_age()
        seen = "never" if age is None else f"{age:.0f}s ago"
        raise MailboxError(f"Plugin is not polling (last heartbeat {seen}). Nothing was sent. "
                           f'Start it on the console with: Plugin "{PLUGIN}"')

    request_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    queued = REQUESTS / f"{request_id}.lua"
    temp = REQUESTS / f"{request_id}.tmp"
    response = RESPONSES / f"{request_id}.json"

    temp.write_text(code, encoding="utf-8")
    os.replace(temp, queued)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response.exists():
            return json.loads(response.read_text(encoding="utf-8", errors="replace"))
        time.sleep(0.05)

    # Take the request back, so it cannot run later when nobody is waiting for it.
    try:
        queued.unlink()
    except FileNotFoundError:
        raise MailboxError(f"No response in {timeout}s. {request_id} was picked up "
                           "and may still be running.") from None
    raise MailboxError(f"No response in {timeout}s. Never picked up, so withdrawn.")


def run(code, timeout):
    try:
        result = execute(code, timeout)
    except MailboxError as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0 if result["ok"] else 1


def status():
    age = heartbeat_age()
    alive = is_alive()
    print("plugin:", "running" if alive else "not running")
    print("last heartbeat:", "never" if age is None else f"{age:.0f}s ago")
    selftest = ROOT / "selftest.json"
    if selftest.exists():
        for line in json.loads(selftest.read_text(errors="replace"))["report"]:
            print(" ", line)
    return 0 if alive else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("file", nargs="?", help="Lua file, or - for stdin")
    run_parser.add_argument("-e", dest="code", help="Lua source")
    run_parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()

    if args.command == "status":
        return status()
    if args.code is not None:
        code = args.code
    elif args.file == "-":
        code = sys.stdin.read()
    elif args.file:
        code = Path(args.file).read_text(encoding="utf-8")
    else:
        parser.error("run needs a file, - or -e")
    return run(code, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
