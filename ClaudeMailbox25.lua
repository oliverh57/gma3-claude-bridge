-- ClaudeMailbox25: run Lua snippets dropped into a local folder, write back the result.
--
-- The grandMA3 2.5 build (Lua 5.5). ClaudeMailbox.lua remains the 2.3 build (Lua 5.4).
-- Every MA version shares this plugins folder and two can run at once, so each build
-- has its own mailbox folder. With a shared one, starting either would stop the other,
-- and a snippet could run in the wrong show.
--
-- Snippets run under Lua 5.5, which differs from 5.4 in three ways that matter here:
--   the control variable of a for loop is read-only, so assigning to it fails to compile
--   global is a keyword in 5.5, so do not use it as a name
--   floats print in full, so 0.1 + 0.2 gives 0.30000000000000004, not 0.3
--
--   Plugin "ClaudeMailbox25"             start, or stop if running
--   Plugin "ClaudeMailbox25" "start"     start with no confirmation
--   Plugin "ClaudeMailbox25" "confirm"   start, asking on the console before each request
--   Plugin "ClaudeMailbox25" "stop"      stop
--
-- Confirm mode is only settable here, on the console. Nothing in the mailbox folder
-- can turn it off.
--
-- Folder layout, under ROOT:
--   requests/<id>.lua      written by the client (via a temp name, then renamed)
--   requests/<id>.running  claimed by this plugin; the rename is the lock
--   responses/<id>.json    result
--   archive/<id>.lua       every request that was run, rejected or found stale
--   mailbox.log            one line per request
--   heartbeat              tick count and time, rewritten every few seconds
--   selftest.json          what this Lua environment can do, written at start
--   owner                  token of the live poller; a poller that loses it exits

local ROOT = os.getenv("HOME") .. "/MALightingTechnology/gma3_library/datapools/plugins/claude_mailbox/v25"
local REQUESTS = ROOT .. "/requests"
local RESPONSES = ROOT .. "/responses"
local ARCHIVE = ROOT .. "/archive"
local LUA_VERSION = "Lua 5.5"
local POLL_SECONDS = 0.5
local HEARTBEAT_EVERY = 6 -- polls

local running = false
local confirm_mode = false
local owner_token = nil
local ticks = 0

-- Kept before any request can shadow them. Requests see tee'd versions, and these
-- are what the tee writes through to.
local real = { Printf = Printf, Echo = Echo, ErrEcho = ErrEcho, ErrPrintf = ErrPrintf, print = print }

local function read_file(path)
  local f = io.open(path, "rb")
  if not f then return nil end
  local data = f:read("a")
  f:close()
  return data
end

-- Temp name then rename, so the client never reads a half-written file.
local function write_file(path, data)
  local temp = path .. ".tmp"
  local f, err = io.open(temp, "wb")
  if not f then return nil, err end
  f:write(data)
  f:close()
  return os.rename(temp, path)
end

local function log_line(text)
  local f = io.open(ROOT .. "/mailbox.log", "ab")
  if not f then return end
  f:write(os.date("%Y-%m-%d %H:%M:%S "), text, "\n")
  f:close()
end

local ESCAPES = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }

local function json_string(value)
  local text = tostring(value):gsub('[%c"\\]', function(c)
    return ESCAPES[c] or string.format("\\u%04x", c:byte())
  end)
  return '"' .. text .. '"'
end

local function json_list(items)
  local out = {}
  for i = 1, #items do out[i] = json_string(items[i]) end
  return "[" .. table.concat(out, ",") .. "]"
end

local function write_response(id, r)
  local body = string.format(
    '{"id":%s,"ok":%s,"status":%s,"output":%s,"returns":%s,"error":%s,"ms":%d}\n',
    json_string(id), tostring(r.ok == true), json_string(r.status),
    json_string(r.output or ""), json_list(r.returns or {}),
    r.error and json_string(r.error) or "null", r.ms or 0)
  return write_file(RESPONSES .. "/" .. id .. ".json", body)
end

-- A request's globals land in its own table, so one snippet cannot leave state behind
-- for the next. MA's API still reads through to _G.
local function request_env(lines)
  local function capture(text) lines[#lines + 1] = text end
  local function formatted(fn)
    return function(fmt, ...)
      local text = string.format(tostring(fmt), ...)
      capture(text)
      fn("%s", text)
    end
  end
  local env = {
    Printf = formatted(real.Printf),
    Echo = formatted(real.Echo),
    ErrEcho = formatted(real.ErrEcho),
    ErrPrintf = formatted(real.ErrPrintf),
    print = function(...)
      local parts = table.pack(...)
      for i = 1, parts.n do parts[i] = tostring(parts[i]) end
      local text = table.concat(parts, "\t", 1, parts.n)
      capture(text)
      real.Printf("%s", text)
    end,
  }
  return setmetatable(env, { __index = _G })
end

local function run_code(id, code)
  local lines = {}
  local chunk, syntax_error = load(code, "=" .. id, "t", request_env(lines))
  if not chunk then
    return { ok = false, status = "syntax_error", error = syntax_error, ms = 0 }
  end
  local started = os.clock()
  local results = table.pack(xpcall(chunk, debug.traceback))
  local ms = math.floor((os.clock() - started) * 1000)
  local output = table.concat(lines, "\n")
  if not results[1] then
    return { ok = false, status = "runtime_error", error = results[2], output = output, ms = ms }
  end
  local returns = {}
  for i = 2, results.n do returns[i - 1] = tostring(results[i]) end
  return { ok = true, status = "ok", output = output, returns = returns, ms = ms }
end

-- Fails closed: a Confirm that errors or returns anything but true is a refusal.
local function approved(id, code)
  if not confirm_mode then return true end
  local ok, answer = pcall(Confirm, "Claude request " .. id, code:sub(1, 400))
  return ok and answer == true
end

local function handle_request(id)
  local claimed = REQUESTS .. "/" .. id .. ".running"
  if not os.rename(REQUESTS .. "/" .. id .. ".lua", claimed) then return end

  local code = read_file(claimed) or ""
  local result
  if approved(id, code) then
    result = run_code(id, code)
  else
    result = { ok = false, status = "rejected", error = "refused on the console" }
  end

  os.rename(claimed, ARCHIVE .. "/" .. id .. ".lua")
  write_response(id, result)
  log_line(string.format("%s %s %dms %dB", id, result.status, result.ms or 0, #code))
end

-- Documented as {name, size, time} tables, but only confirmed live on 2.3, so also
-- accept a plain string.
local function entry_name(entry)
  if type(entry) == "table" then return entry.name or entry.Name or entry[1] end
  return entry
end

local function pending_ids()
  local ids = {}
  for _, entry in ipairs(DirList(REQUESTS .. "/", "*.lua") or {}) do
    local name = entry_name(entry)
    local id = type(name) == "string" and name:match("([%w_%-]+)%.lua$")
    if id then ids[#ids + 1] = id end
  end
  table.sort(ids)
  return ids
end

-- Anything already queued was written before this start, possibly long ago, and was
-- never approved by anyone watching. Refuse it rather than run it.
local function reject_stale_requests()
  for _, id in ipairs(pending_ids()) do
    if os.rename(REQUESTS .. "/" .. id .. ".lua", ARCHIVE .. "/" .. id .. ".stale.lua") then
      write_response(id, { ok = false, status = "stale", error = "queued before the plugin started" })
      log_line(id .. " stale")
    end
  end
end

local function poll()
  if not running or read_file(ROOT .. "/owner") ~= owner_token then
    running = false
    return
  end
  ticks = ticks + 1
  local ok, err = pcall(function()
    for _, id in ipairs(pending_ids()) do handle_request(id) end
    if ticks % HEARTBEAT_EVERY == 1 then
      write_file(ROOT .. "/heartbeat", string.format("%d %d", ticks, os.time()))
    end
  end)
  if not ok then
    log_line("poll error: " .. tostring(err))
    real.ErrEcho("%s", "ClaudeMailbox25 poll error: " .. tostring(err))
  end
  Timer(poll, POLL_SECONDS, 1)
end

-- Each probe is independent, so one failure still leaves the others in the report.
local function self_test()
  local report = {}
  local function probe(name, fn)
    local ok, value = pcall(fn)
    report[#report + 1] = name .. " = " .. (ok and "ok: " or "FAILED: ") .. tostring(value)
  end
  probe("lua", function() return _VERSION end)
  probe("ma_version", function() return Version() end)
  probe("host_os", function() return HostOS() end)
  probe("plugins_path", function() return GetPath("plugins") end)
  probe("libraries", function()
    local have = {}
    for _, name in ipairs({ "io", "os", "debug", "coroutine", "package" }) do
      have[#have + 1] = name .. "=" .. type(_G[name])
    end
    return table.concat(have, " ")
  end)
  probe("io_roundtrip", function()
    local path = ROOT .. "/selftest.tmp"
    assert(write_file(path, "written by selftest"))
    local data = assert(read_file(path))
    os.remove(path)
    return data
  end)
  probe("load_with_env", function()
    local env = setmetatable({}, { __index = _G })
    local chunk = assert(load("leak = 1 + 1; return leak", "=selftest", "t", env))
    return string.format("returned %s, leaked into _G: %s", chunk(), tostring(rawget(_G, "leak") ~= nil))
  end)
  probe("ma_api_in_env", function()
    local env = setmetatable({}, { __index = _G })
    return assert(load('return type(Cmd) .. " " .. type(Timer) .. " " .. type(DirList)', "=selftest", "t", env))()
  end)
  probe("for_variable", function()
    return load("for i = 1, 1 do i = 2 end") and "assignable" or "read-only"
  end)
  probe("global_as_name", function()
    return load("local global = 1") and "allowed" or "reserved"
  end)
  probe("float_text", function() return 0.1 + 0.2 end)
  probe("dirlist_entry", function()
    local list = DirList(ROOT .. "/", "*") or {}
    local first = list[1]
    if type(first) ~= "table" then return #list .. " entries, first: " .. tostring(first) end
    local keys = {}
    for k, v in pairs(first) do keys[#keys + 1] = tostring(k) .. "=" .. tostring(v) end
    return #list .. " entries, first: {" .. table.concat(keys, ", ") .. "}"
  end)
  return write_file(ROOT .. "/selftest.json", "{\"report\":" .. json_list(report) .. "}\n")
end

local function start(with_confirm)
  -- Any MA version can import this from the shared plugins folder. A build on the wrong
  -- Lua must not answer the 2.5 mailbox, or requests would run in that version's show.
  if _VERSION ~= LUA_VERSION then
    real.ErrEcho("%s", "ClaudeMailbox25 needs " .. LUA_VERSION .. ", this is " .. _VERSION .. ". Nothing started.")
    return
  end
  confirm_mode = with_confirm
  if running then
    real.Printf("%s", "ClaudeMailbox25 already running, confirm mode " .. tostring(confirm_mode))
    return
  end
  local wrote, err = self_test()
  if not wrote then
    real.ErrEcho("%s", "ClaudeMailbox25 cannot write to " .. ROOT .. ": " .. tostring(err))
    return
  end
  owner_token = string.format("%d-%d", os.time(), math.random(1, 1000000000))
  write_file(ROOT .. "/owner", owner_token)
  reject_stale_requests()
  running = true
  ticks = 0
  log_line("started, confirm mode " .. tostring(confirm_mode))
  real.Printf("%s", "ClaudeMailbox25 running, confirm mode " .. tostring(confirm_mode))
  Timer(poll, POLL_SECONDS, 1)
end

local function stop()
  running = false
  os.remove(ROOT .. "/owner")
  log_line("stopped")
  real.Printf("%s", "ClaudeMailbox25 stopped")
end

return function(_, argument)
  if argument == "stop" then
    stop()
  elseif argument == "confirm" then
    start(true)
  elseif argument == "start" then
    start(false)
  elseif running then
    stop()
  else
    start(false)
  end
end
