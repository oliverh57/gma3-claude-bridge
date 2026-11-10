#!/usr/bin/env python3
"""Local, read-only dashboard for the ClaudeMailbox bridge. Standard library only.

  dashboard/server.py                 serve on http://127.0.0.1:8733
  dashboard/server.py --port 9000
  dashboard/server.py --no-console    read mailbox files only, send nothing to MA

History comes from the mailbox folders. What is live on the console comes from a small
read-only snippet sent through the mailbox. Those requests are named dash_<...> so they
stay out of the command history, and their archive and response files are removed once
read. A line per poll still lands in mailbox.log. Polling drops to once a minute when no
browser has the page open.

The server has no route that writes to MA, and no route that takes Lua.
"""
import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE))
from ma3_mcp import LUA_PRELUDE  # noqa: E402

MA_HOME = BASE.parents[3]
STATE_FILE = HERE / "state.json"

HEARTBEAT_STALE_SECONDS = 15  # same limit as ma3.py
SNAPSHOT_TIMEOUT = 5
POLL_VIEWED = 10
POLL_IDLE = 60
VIEWER_GONE_AFTER = 30
SAME_SESSION_GAP = 90  # longer than this between sightings, and the show is unknown in between
FILE_STAMP_SKEW = 3600  # 2.3.2 stamped show files an hour ahead of the system clock on 2026-11-04

SNAPSHOT_LUA = LUA_PRELUDE + r'''
local function safe(fn)
  local ok, value = pcall(fn)
  if ok then return value end
end

local pools = ShowData().DataPools.Default
local snap = {
  show = safe(function() return Root().MAnetSocket.Showfile end),
  version = safe(Version),
  host = safe(HostType),
  shows_path = safe(function() return GetPath("shows") end),
  user = safe(function() return CurrentUser().Name end),
  profile = safe(function() return CurrentProfile().Name end),
  selection = safe(SelectionCount),
  sequence = safe(function() local s = SelectedSequence(); return { no = s.No, name = s.Name } end),
  page = safe(function() local p = CurrentExecPage(); return { no = p.No, name = p.Name } end),
  fixtures = safe(function()
    local n = 0
    for _, stage in ipairs(Patch().Stages:Children()) do n = n + #stage.Fixtures:Children() end
    return n
  end),
}

snap.pools = {}
for _, pool in ipairs(pools:Children()) do
  if pool.Name ~= "PresetPools" then
    snap.pools[#snap.pools + 1] = { name = pool.Name, count = #pool:Children() }
  end
end
local views = safe(function() return #CurrentProfile().Views:Children() end)
if views then snap.pools[#snap.pools + 1] = { name = "Views", count = views } end

snap.presets = {}
for _, pool in ipairs(pools.PresetPools:Children()) do
  local n = #pool:Children()
  if n > 0 then snap.presets[#snap.presets + 1] = { no = pool.No, name = pool.Name, count = n } end
end
return encode(snap)
'''

ID_STAMP = re.compile(r"^(\d{8}_\d{6})_")
COMMENT = re.compile(r"--[^\n]*")
CMD_TEMPLATE = re.compile(
    r"""\bCmd(?:Indirect(?:Wait)?)?\s*\(\s*(?:string\.format\(\s*)?(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')""")
CMD_CALL = re.compile(r"\bCmd(?:Indirect(?:Wait)?)?\s*\(")
OBJECT_WRITE = re.compile(
    r":(?:Set|SetChildren|Create|Acquire|Delete|Copy|Insert|Append|Import|Remove)\s*\(|\bCreateUndo\b"
    r"|[\w\]\)]\.[A-Z]\w*\s*=[^=]")
DRY_FLAG = re.compile(r"\b(?:DRY|DRY_RUN|dry|dry_run)\s*=\s*true\b")
MOVE_ARGS = re.compile(r'local kind, from_no, to_no, dry_run = "(\w+)", (\d+), (\d+), (true|false)')
POOL_ARG = re.compile(r'find_pool\("([^"]+)"\)')
ADDRESS_ARG = re.compile(r'ObjectList\("([^"]+)"\)')
READ_VERBS = {"version", "list", "info", "echo", "help"}
TOUCHED = re.compile(r"\b(Sequences|Macros|PresetPools|Groups|Layouts|Pages|Timecodes|Plugins|Views|UserProfiles"
                     r"|Appearances|Patch|Stages|Fixtures|Filters|MAtricks|Worlds)\b")


def one_line(text, limit=140):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def stamp_of(request_id):
    match = ID_STAMP.match(request_id)
    return datetime.strptime(match[1], "%Y%m%d_%H%M%S").timestamp() if match else None


def read_response(path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def describe_source(source):
    """Work out what a snippet was from its text. This reads the source, not what ran."""
    code = COMMENT.sub("", source)
    templates = [a or b for a, b in CMD_TEMPLATE.findall(code)]
    cmd_calls = len(CMD_CALL.findall(code))
    only_reads = len(templates) == cmd_calls and all(
        t.split()[0].lower() in READ_VERBS for t in templates if t.split())
    templates = list(dict.fromkeys(templates))

    move = MOVE_ARGS.search(source)
    if move:
        kind, from_no, to_no, dry = move.groups()
        return {"tool": "move_object", "kind": "read" if dry == "true" else "change",
                "summary": f"{'Dry run: ' if dry == 'true' else ''}Move {kind} {from_no} At {kind} {to_no}",
                "cmds": [f"Move {kind} {from_no} At {kind} {to_no}"]}
    if "local function describe(o, depth)" in source:
        address = ADDRESS_ARG.search(source)
        return {"tool": "dump_object", "kind": "read", "cmds": [],
                "summary": f"Read {address[1] if address else 'object'}"}
    if "{ no = o.No, name = o.Name, class = o:GetClass() }" in source:
        pool = POOL_ARG.search(source)
        return {"tool": "list_pool", "kind": "read", "cmds": [],
                "summary": f"List {pool[1] if pool else 'pool'}"}

    writes = (cmd_calls > 0 and not only_reads) or bool(OBJECT_WRITE.search(code))
    if DRY_FLAG.search(code):
        writes = False
    if templates:
        extra = f"  (+{len(templates) - 1} more)" if len(templates) > 1 else ""
        summary = one_line(templates[0], 100) + extra
    else:
        touched = list(dict.fromkeys(TOUCHED.findall(code)))
        leading = next((line.strip() for line in source.splitlines() if line.strip()), "")
        summary = ("Lua on " + ", ".join(touched[:4])) if touched else one_line(leading.lstrip("- "), 100)
    return {"tool": "run_lua", "kind": "change" if writes else "read", "summary": summary, "cmds": templates}


def result_preview(response):
    if response is None:
        return ""
    if response.get("error"):
        lines = str(response["error"]).strip().splitlines()
        return one_line(lines[0] if lines else "")
    returns = response.get("returns") or []
    text = returns[0] if returns else response.get("output") or ""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return one_line(text)
    if isinstance(value, list):
        return f"{len(value)} objects"
    if isinstance(value, dict):
        if "dry_run" in value:
            refs = len(value.get("text_references") or [])
            state = "dry run" if value["dry_run"] else "moved" if value.get("moved") else "move not confirmed"
            return f"{value.get('name', '')}: {state}, {refs} text references"
        if "addr" in value:
            return f"{value.get('name', '')} ({value.get('class', '')}), {value.get('child_count', 0)} children"
    return one_line(text)


class Store:
    """Show sightings and last snapshots, kept so a stopped bridge still has something to say."""

    def __init__(self):
        self.lock = threading.Lock()
        try:
            data = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            data = {}
        self.sightings = data.get("sightings", {})
        self.snapshots = data.get("snapshots", {})
        self.saves = data.get("saves", {})

    def note_save(self, key, show_file, now):
        """Keep the time a show file was seen to change, which needs no trust in MA's stamp."""
        slot = f"{key}/{show_file['name']}"
        with self.lock:
            known = self.saves.get(slot)
            if known is None or known["stamp"] != show_file["saved"]:
                last = self.snapshots.get(key)
                watching = known is not None and last and now - last["at"] <= SAME_SESSION_GAP
                self.saves[slot] = {"stamp": show_file["saved"], "seen": now if watching else None}

    def seen_save(self, key, show_file):
        with self.lock:
            known = self.saves.get(f"{key}/{show_file['name']}")
        return known["seen"] if known and known["stamp"] == show_file["saved"] else None

    def record(self, key, snapshot, seen_at):
        with self.lock:
            self.snapshots[key] = {"at": seen_at, "data": snapshot}
            spans = self.sightings.setdefault(key, [])
            show = snapshot.get("show")
            if spans and spans[-1]["show"] == show and seen_at - spans[-1]["to"] <= SAME_SESSION_GAP:
                spans[-1]["to"] = seen_at
            else:
                spans.append({"show": show, "from": seen_at, "to": seen_at})
            temp = STATE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps({"sightings": self.sightings, "snapshots": self.snapshots,
                                        "saves": self.saves}))
            os.replace(temp, STATE_FILE)

    def show_at(self, key, when):
        with self.lock:
            for span in reversed(self.sightings.get(key, [])):
                if span["from"] - POLL_VIEWED <= when <= span["to"] + POLL_VIEWED:
                    return span["show"]
        return None


class Bridge:
    def __init__(self, key, label, root, plugin, shows, store):
        self.key, self.label, self.root, self.plugin = key, label, root, plugin
        self.shows, self.store = shows, store
        self.parsed = {}
        self.parse_lock = threading.Lock()
        self.snapshot_error = None
        self.paused_owner = None  # owner token we stopped polling for, after an unanswered request

    # ---- files

    def text(self, name):
        try:
            return (self.root / name).read_text(errors="replace")
        except OSError:
            return None

    def heartbeat(self):
        try:
            return int((self.text("heartbeat") or "").split()[1])
        except (IndexError, ValueError):
            return None

    def log_state(self):
        log = self.text("mailbox.log") or ""
        started = log.rfind(" started, confirm mode ")
        stopped = log.rfind(" stopped\n")
        confirm = None
        if started >= 0:
            confirm = log[started:started + 30].split()[-1] == "true"
        return {"confirm": confirm, "stop_logged": stopped > started,
                "started_at": log[max(0, started - 19):started] if started >= 19 else None}

    def status(self):
        beat, log = self.heartbeat(), self.log_state()
        owner = self.text("owner")
        if beat is None:
            state = "never"
        elif time.time() - beat < HEARTBEAT_STALE_SECONDS and owner is not None:
            state = "live"
        elif owner is None or log["stop_logged"]:
            state = "stopped"
        else:
            state = "lost"
        return state, beat, log, owner

    def commands(self):
        with self.parse_lock:
            found = []
            try:
                entries = list(os.scandir(self.root / "archive"))
            except OSError:
                entries = []
            for entry in entries:
                name = entry.name
                if not name.endswith(".lua") or name.startswith("dash_"):
                    continue
                request_id = name.removesuffix(".lua").removesuffix(".stale")
                item = self.parsed.get(request_id) or self.parse(request_id, Path(entry.path))
                found.append(item)
            return found

    def parse(self, request_id, path):
        source = path.read_text(encoding="utf-8", errors="replace")
        response = read_response(self.root / "responses" / f"{request_id}.json")
        item = {"bridge": self.key, "id": request_id, "ts": stamp_of(request_id) or path.stat().st_mtime,
                "bytes": len(source.encode()), "source": source, **describe_source(source)}
        item.update(status=(response or {}).get("status", "pending"), ms=(response or {}).get("ms"),
                    result=result_preview(response))
        if response is not None:
            self.parsed[request_id] = item  # archive files never change once answered
        return item

    def pending(self):
        try:
            return sorted(n for n in os.listdir(self.root / "requests")
                          if n.endswith((".lua", ".running")) and not n.startswith("dash_"))
        except OSError:
            return []

    def show_file(self, name):
        path = self.shows / f"{name}.show"
        try:
            info = path.stat()
        except OSError:
            return None
        return {"name": name, "saved": info.st_mtime, "size": info.st_size}

    def newest_show_file(self):
        try:
            files = [e for e in os.scandir(self.shows) if e.name.endswith(".show")]
        except OSError:
            return None
        if not files:
            return None
        newest = max(files, key=lambda e: e.stat().st_mtime)
        return self.show_file(newest.name.removesuffix(".show"))

    # ---- console

    def sweep(self):
        """Remove the dashboard's own finished requests. Only ever touches dash_ files."""
        for folder in ("archive", "responses"):
            for path in (self.root / folder).glob("dash_*"):
                path.unlink(missing_ok=True)

    def take_snapshot(self):
        request_id = f"dash_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        requests = self.root / "requests"
        queued, temp = requests / f"{request_id}.lua", requests / f"{request_id}.tmp"
        response = self.root / "responses" / f"{request_id}.json"
        temp.write_text(SNAPSHOT_LUA, encoding="utf-8")
        os.replace(temp, queued)

        deadline = time.monotonic() + SNAPSHOT_TIMEOUT
        while time.monotonic() < deadline:
            if response.exists():
                reply = read_response(response)
                if reply is None:
                    time.sleep(0.05)
                    continue
                self.sweep()
                if not reply.get("ok"):
                    raise RuntimeError(f"{reply.get('status')}: {reply.get('error')}")
                return json.loads(reply["returns"][0])
            time.sleep(0.05)
        try:
            queued.unlink()
        except FileNotFoundError:
            # Picked up and never answered. Confirm mode can be switched on with no log line,
            # so this may be a dialog on the console. Do not send another one.
            self.paused_owner = self.text("owner")
            raise RuntimeError("The console took the request and did not answer. "
                               "Live polling is paused until the plugin restarts.") from None
        raise RuntimeError("The console did not pick up the request.")

    def poll_once(self):
        state, _, log, owner = self.status()
        if state != "live":
            self.snapshot_error = None
            return
        if log["confirm"]:
            self.snapshot_error = "Confirm mode is on, so live polling is off: each poll would ask on the console."
            return
        if self.paused_owner is not None:
            if owner == self.paused_owner:
                return
            self.paused_owner = None
        try:
            snapshot = self.take_snapshot()
        except (OSError, ValueError, RuntimeError) as error:
            self.snapshot_error = str(error)
            return
        self.snapshot_error = None
        now = time.time()
        show_file = self.show_file(snapshot["show"]) if snapshot.get("show") else None
        if show_file:
            self.store.note_save(self.key, show_file, now)
        self.store.record(self.key, snapshot, now)

    def poll_forever(self, app):
        self.sweep()
        while True:
            self.poll_once()
            viewed = time.time() - app.last_view < VIEWER_GONE_AFTER
            wait_until = time.monotonic() + (POLL_VIEWED if viewed else POLL_IDLE)
            while time.monotonic() < wait_until:
                time.sleep(1)
                if not viewed and time.time() - app.last_view < VIEWER_GONE_AFTER:
                    break  # someone opened the page, so catch up now

    # ---- API

    def state(self, commands):
        state, beat, log, _ = self.status()
        with self.store.lock:
            saved = self.store.snapshots.get(self.key)
        snapshot = saved["data"] if saved else None
        file_info = self.show_file(snapshot["show"]) if snapshot and snapshot.get("show") else None
        since_save = None
        if file_info:
            file_info["seen"] = self.store.seen_save(self.key, file_info)
        if file_info and state == "live":
            # A stamp nobody saw change may be an hour ahead. Assume so: a needless warning
            # is better than saying a change is saved when it is not.
            cutoff = file_info["seen"] or file_info["saved"] - FILE_STAMP_SKEW
            since_save = sum(1 for c in commands if c["bridge"] == self.key and c["kind"] == "change"
                             and c["ts"] > cutoff and c["show"] in (None, snapshot["show"]))
        selftest = None
        try:
            selftest = json.loads(self.text("selftest.json") or "null")
        except ValueError:
            pass
        return {
            "key": self.key, "label": self.label, "plugin": self.plugin, "state": state,
            "heartbeat": beat, "confirm": log["confirm"], "started": log["started_at"],
            "pending": self.pending(), "snapshot": snapshot, "snapshot_at": saved["at"] if saved else None,
            "snapshot_error": self.snapshot_error, "show_file": file_info,
            "newest_show_file": None if file_info else self.newest_show_file(),
            "changes_since_save": since_save, "selftest": (selftest or {}).get("report", []),
        }


FILE_LABELS = {
    "move_journal.jsonl": "Sequence renumber journal",
    "sequence_move_plan.csv": "Sequence renumber plan",
    "preset_move_journal.jsonl": "Preset renumber journal",
    "preset_move_plan.csv": "Preset renumber plan",
}


def data_files():
    """Work the bridge has left on disk: journals, plans and backups beside the mailbox."""
    out = []
    for entry in sorted(os.scandir(BASE), key=lambda e: e.name):
        name = entry.name
        if entry.is_dir() and name == "song_view_backup":
            count = sum(1 for _ in os.scandir(entry.path))
            out.append({"name": name, "label": "Song view backups (XML)", "rows": count, "unit": "files",
                        "modified": entry.stat().st_mtime, "size": None, "readable": False})
        elif entry.is_file() and name.endswith((".jsonl", ".csv", ".json")) and name != "selftest.json":
            label = FILE_LABELS.get(name)
            if label is None and name.startswith("name_audit_journal_"):
                label = "Name audit: " + name.removeprefix("name_audit_journal_").removesuffix(".json")
            rows = None
            if name.endswith((".jsonl", ".csv")):
                with open(entry.path, "rb") as f:
                    rows = sum(1 for line in f if line.strip()) - (1 if name.endswith(".csv") else 0)
            info = entry.stat()
            out.append({"name": name, "label": label or name, "rows": rows, "unit": "rows",
                        "modified": info.st_mtime, "size": info.st_size, "readable": True})
    return out


class App:
    def __init__(self, console):
        self.store = Store()
        self.console = console
        self.last_view = 0.0
        self.bridges = {
            "v25": Bridge("v25", "onPC 2.5", BASE / "v25", "ClaudeMailbox25",
                          MA_HOME / "gma3_2.5.1/shared/shows", self.store),
            "v23": Bridge("v23", "onPC 2.3", BASE, "ClaudeMailbox",
                          MA_HOME / "gma3_2.3.2/shared/shows", self.store),
        }

    def start(self):
        if self.console:
            for bridge in self.bridges.values():
                threading.Thread(target=bridge.poll_forever, args=(self,), daemon=True).start()

    def commands(self):
        out = []
        for bridge in self.bridges.values():
            for item in bridge.commands():
                out.append({**item, "show": self.store.show_at(bridge.key, item["ts"])})
        out.sort(key=lambda c: (c["ts"], c["id"]), reverse=True)
        return out

    def state(self):
        self.last_view = time.time()
        commands = self.commands()
        times = [c["ms"] for c in commands if c["ms"] is not None]
        return {
            "now": time.time(), "console": self.console,
            "rev": f"{len(commands)}:{commands[0]['id'] if commands else ''}:"
                   f"{sum(c['status'] == 'pending' for c in commands)}",
            "bridges": [b.state(commands) for b in self.bridges.values()],
            "median_ms": statistics.median(times) if times else None,
        }

    def command_list(self, query):
        needle = query.lower()
        rows = []
        for command in self.commands():
            if needle and not any(needle in str(command.get(k, "")).lower()
                                  for k in ("source", "result", "summary", "id", "show")):
                continue
            rows.append({k: v for k, v in command.items() if k != "source"})
        return rows

    def command_detail(self, key, request_id):
        bridge = self.bridges.get(key)
        if bridge is None or not re.fullmatch(r"[\w-]+", request_id) or request_id.startswith("dash_"):
            return None
        item = next((c for c in bridge.commands() if c["id"] == request_id), None)
        if item is None:
            return None
        return {**item, "show": self.store.show_at(key, item["ts"]),
                "response": read_response(bridge.root / "responses" / f"{request_id}.json")}

    def file_preview(self, name, limit=300):
        if not any(f["name"] == name and f["readable"] for f in data_files()):
            return None
        with open(BASE / name, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return {"name": name, "total": len(lines), "text": "".join(lines[:limit])}


class Handler(BaseHTTPRequestHandler):
    app = None
    allowed_hosts = ()

    def log_message(self, *args):
        pass

    def send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Stops another website reaching this server by pointing its own name at 127.0.0.1.
        if self.headers.get("Host") not in self.allowed_hosts:
            return self.send(403, {"error": "bad host"})
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        app = self.app
        if url.path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self.send(200, app.state())
        if url.path == "/api/commands":
            return self.send(200, app.command_list(query.get("q", "")))
        if url.path == "/api/command":
            found = app.command_detail(query.get("bridge", ""), query.get("id", ""))
            return self.send(200, found) if found else self.send(404, {"error": "not found"})
        if url.path == "/api/files":
            return self.send(200, data_files())
        if url.path == "/api/file":
            found = app.file_preview(query.get("name", ""))
            return self.send(200, found) if found else self.send(404, {"error": "not found"})
        return self.send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8733)
    parser.add_argument("--no-console", action="store_true", help="send nothing to MA")
    args = parser.parse_args()

    app = App(console=not args.no_console)
    Handler.app = app
    Handler.allowed_hosts = (f"127.0.0.1:{args.port}", f"localhost:{args.port}")
    app.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Claude Bridge dashboard on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
