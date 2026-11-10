#!/usr/bin/env python3
"""MCP server (stdio, standard library only) over the ClaudeMailbox plugin.

Each tool is a Lua snippet sent through ma3.execute, so adding or changing a tool here
never needs the plugin re-imported into MA.
"""
import json
import sys

import ma3

# Snippets return one JSON string rather than printing it, so a big dump does not
# flood the System Monitor.
LUA_PRELUDE = r'''
local function encode(v)
  local t = type(v)
  if t == "table" then
    local out = {}
    if #v > 0 or next(v) == nil then
      for i = 1, #v do out[i] = encode(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    for k, item in pairs(v) do out[#out + 1] = encode(tostring(k)) .. ":" .. encode(item) end
    return "{" .. table.concat(out, ",") .. "}"
  elseif t == "number" or t == "boolean" then
    return tostring(v)
  elseif v == nil then
    return "null"
  end
  local text = tostring(v):gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end)
  return '"' .. text .. '"'
end

local function find_pool(name)
  local pools = ShowData().DataPools.Default
  for _, owner in ipairs({ pools, CurrentProfile() }) do
    local ok, pool = pcall(function() return owner[name] end)
    if ok and pool then return pool end
  end
  error("no pool called " .. name)
end
'''

LIST_POOL = r'''
local out = {}
for _, o in ipairs(find_pool(%(pool)s):Children()) do
  out[#out + 1] = { no = o.No, name = o.Name, class = o:GetClass() }
end
return encode(out)
'''

DUMP_OBJECT = r'''
local function value_of(o, name)
  local ok, v = pcall(o.Get, o, name)
  if not ok or v == nil or v == "" then return nil end
  if type(v) == "userdata" then
    local got, addr = pcall(o.ToAddr, v)
    return got and addr or tostring(v)
  end
  return type(v) == "number" and v or tostring(v)
end

local function describe(o, depth)
  local d = { addr = o:ToAddr(), class = o:GetClass(), name = o.Name }
  local kids = o:Children()
  d.child_count = #kids
  if depth < 0 then return d end
  d.properties = {}
  for i = 0, o:PropertyCount() - 1 do
    local name = o:PropertyName(i)
    d.properties[name] = value_of(o, name)
  end
  if depth > 0 then
    d.children = {}
    for i = 1, math.min(#kids, %(max_children)d) do
      d.children[i] = describe(kids[i], depth > 1 and depth - 1 or -1)
    end
  end
  return d
end

local found = ObjectList(%(address)s)
if #found == 0 then error("nothing at " .. %(address)s) end
return encode(describe(found[1], %(depth)d))
'''

# Handles follow a move, confirmed on 2.3.2, but command text does not. Matching is
# best effort: it finds "Sequence 12", "Seq 12" and "Sequence 12.3" but not the 12 in
# "Sequence 10 Thru 15" or "Sequence 3 + 12".
MOVE_OBJECT = r'''
local kind, from_no, to_no, dry_run = %(kind)s, %(from_no)d, %(to_no)d, %(dry_run)s
local pool = find_pool(%(pool)s)
local source = pool:Ptr(from_no)
if source == nil then error(kind .. " " .. from_no .. " does not exist") end
if pool:Ptr(to_no) ~= nil then error(kind .. " " .. to_no .. " is occupied; refusing to overwrite") end

local wanted = kind:lower()
local function mentions(text)
  for word, number in text:lower():gmatch("(%%a+)%%s+(%%d+)") do
    if #word >= 3 and wanted:sub(1, #word) == word and tonumber(number) == from_no then return true end
  end
  return false
end

local function command_of(o)
  local ok, text = pcall(function() return o.Command or o.Cmd end)
  return ok and type(text) == "string" and text or ""
end

local refs = {}
local function check(o)
  local text = command_of(o)
  if text ~= "" and mentions(text) then refs[#refs + 1] = { addr = o:ToAddr(), text = text } end
end

local pools = ShowData().DataPools.Default
for _, macro in ipairs(pools.Macros:Children()) do
  for _, line in ipairs(macro:Children()) do check(line) end
end
for _, sequence in ipairs(pools.Sequences:Children()) do
  for _, cue in ipairs(sequence:Children()) do
    check(cue)
    for _, part in ipairs(cue:Children()) do check(part) end
  end
end

local result = { name = source.Name, dry_run = dry_run, text_references = refs }
if not dry_run then
  local name = source.Name
  result.command = (Cmd(string.format("Move %%s %%d At %%s %%d", kind, from_no, kind, to_no)))
  local moved = pool:Ptr(to_no)
  result.moved = pool:Ptr(from_no) == nil and moved ~= nil and moved.Name == name
end
return encode(result)
'''

MOVE_KINDS = {
    "Sequence": "Sequences", "Macro": "Macros", "Plugin": "Plugins", "Group": "Groups",
    "Layout": "Layouts", "Page": "Pages", "Timecode": "Timecodes", "Timer": "Timers",
    "Quickey": "Quickeys", "MAtricks": "MAtricks", "Filter": "Filters", "World": "Worlds",
    "View": "Views",
}


def lua_string(text):
    """JSON string escapes are valid Lua, apart from \\u, which ensure_ascii=False avoids."""
    return json.dumps(text, ensure_ascii=False)


def run_snippet(template, timeout=30, **values):
    response = ma3.execute(LUA_PRELUDE + template % values, timeout)
    if not response["ok"]:
        raise ma3.MailboxError(f"{response['status']}: {response['error']}")
    return response["returns"][0]


def run_lua(args):
    return json.dumps(ma3.execute(args["code"], args.get("timeout", 15)))


def list_pool(args):
    return run_snippet(LIST_POOL, pool=lua_string(args["pool"]))


def dump_object(args):
    return run_snippet(DUMP_OBJECT, address=lua_string(args["address"]),
                       depth=int(args.get("depth", 1)), max_children=int(args.get("max_children", 200)))


def move_object(args):
    kind = args["kind"]
    if kind not in MOVE_KINDS:
        raise ma3.MailboxError(f"kind must be one of {sorted(MOVE_KINDS)}")
    if int(args["from_no"]) == int(args["to_no"]):
        raise ma3.MailboxError("from_no and to_no are the same slot")
    return run_snippet(MOVE_OBJECT, kind=lua_string(kind), pool=lua_string(MOVE_KINDS[kind]),
                       from_no=int(args["from_no"]), to_no=int(args["to_no"]),
                       dry_run="true" if args.get("dry_run", True) else "false")


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required}


TOOLS = {
    "run_lua": (run_lua, (
        "Run Lua in the live grandMA3 show. Returns status, captured print/Printf output and "
        "return values. Prefer Cmd() for edits: Cmd changes can be undone with Oops, direct "
        "property writes cannot. Cmd output is not captured."),
        schema({"code": {"type": "string"}, "timeout": {"type": "number", "default": 15}}, ["code"])),
    "list_pool": (list_pool, (
        "List the objects that exist in a pool, as no, name and class. Pool is a name such as "
        "Sequences, Macros, Pages, Layouts, Plugins, or Views from the current user profile."),
        schema({"pool": {"type": "string"}}, ["pool"])),
    "dump_object": (dump_object, (
        "Read one object's properties and children by address, such as 'Sequence 1', "
        "'Sequence 1 Cue 1' or 'View 1'. Children are listed with their own properties "
        "down to depth levels. Empty properties are left out."),
        schema({"address": {"type": "string"}, "depth": {"type": "integer", "default": 1},
                "max_children": {"type": "integer", "default": 200}}, ["address"])),
    "move_object": (move_object, (
        "Move a pool object to an empty slot with MA's Move command, which can be undone with "
        "Oops. Refuses an occupied target. Dry run by default: set dry_run false to move. "
        "Executor assignments follow the move, but macro lines and cue commands that name the "
        "old number do not. Those are returned in text_references, best effort: numbers "
        "inside ranges such as 'Thru' are not detected."),
        schema({"kind": {"type": "string", "enum": sorted(MOVE_KINDS)},
                "from_no": {"type": "integer"}, "to_no": {"type": "integer"},
                "dry_run": {"type": "boolean", "default": True}},
               ["kind", "from_no", "to_no"])),
}


def call_tool(params):
    handler = TOOLS[params["name"]][0]
    try:
        return {"content": [{"type": "text", "text": handler(params.get("arguments", {}))}]}
    except (ma3.MailboxError, KeyError, ValueError) as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}


def handle(message):
    method, params = message.get("method"), message.get("params", {})
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "grandma3", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": description, "inputSchema": input_schema}
                          for name, (_, description, input_schema) in TOOLS.items()]}
    if method == "tools/call":
        return call_tool(params)
    return None


def main():
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue  # notification
        result = handle(message)
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        if result is None:
            reply["error"] = {"code": -32601, "message": f"unknown method {message.get('method')}"}
        else:
            reply["result"] = result
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
