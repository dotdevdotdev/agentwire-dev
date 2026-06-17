-- AgentWire — two-key push-to-talk (Hammerspoon)
-- ============================================================================
-- Drop this in ~/.hammerspoon/init.lua (or `require` it from yours) and reload
-- (Hammerspoon menu > Reload Config, or ⌘⇧R).
--
-- Two keys, that's the whole interface:
--
--   ⌥Space   — talk to the TAB TARGET. Tap to start recording, tap again to
--              stop + transcribe + send to whichever session the portal is
--              focused on (read from ~/.agentwire/active-session). Falls back
--              to a default session if the file is missing/empty.
--
--   ⌥⌘Space  — PICK-AND-TALK. Tap to open a session chooser AND start
--              recording at the same time — talk while you pick from the live
--              list (type to filter / arrow / click). Tap ⌥⌘Space again, or
--              press Enter / click a row, to stop + send to the highlighted
--              session. Esc / dismiss cancels with no send. You pick visually,
--              so it can never misroute — there's no voice name-matching.
--
-- It shells out to the agentwire CLI:
--   agentwire listen start              -- begin recording (async)
--   agentwire listen stop -s <session>  -- stop, transcribe, send to session
--   agentwire listen cancel             -- stop and discard (no send)
--   agentwire list --sessions --json    -- live session list, as JSON
-- ============================================================================

require("hs.ipc")  -- needed for the `hs` CLI; first load may prompt to install

-- ── Config ──────────────────────────────────────────────────────────────────
-- Hammerspoon launches with a stripped PATH (/usr/bin:/bin), so the agentwire
-- CLI and its child processes (ffmpeg, etc.) won't resolve. Prepend the usual
-- Homebrew + user-local bin dirs.
local PATH = "/opt/homebrew/bin:" .. os.getenv("HOME") .. "/.local/bin:"
    .. (os.getenv("PATH") or "/usr/bin:/bin")
local agentwire = os.getenv("HOME") .. "/.local/bin/agentwire"

-- The portal writes the focused session here (see docs: active-session contract).
local ACTIVE_FILE = os.getenv("HOME") .. "/.agentwire/active-session"
local DEFAULT_TARGET = "agentwire"   -- used when the shadow file is missing/empty

local SAFETY_SECS = 120   -- force-stop a capture left running this long

-- ── State ─────────────────────────────────────────────────────────────────
-- A single capture is ever in flight. `mode` is the state machine:
--   nil    — idle
--   "tab"  — ⌥Space recording, will send to the tab target
--   "pick" — ⌥⌘Space recording, chooser open
local mode = nil
local chooser = nil
local safetyTimer = nil

-- ── CLI helpers ─────────────────────────────────────────────────────────────

-- Fire-and-forget. Optional onDone() runs when the process exits.
local function run(args, onDone)
    local cmd = "PATH=" .. PATH .. " " .. agentwire .. " " .. table.concat(args, " ")
    hs.task.new("/bin/bash", function()
        if onDone then onDone() end
    end, {"-c", cmd}):start()
end

-- Like run(), but hands stdout (trimmed) back to onDone(stdout).
local function runCapture(args, onDone)
    local cmd = "PATH=" .. PATH .. " " .. agentwire .. " " .. table.concat(args, " ")
    hs.task.new("/bin/bash", function(_, stdout)
        onDone((stdout or ""):gsub("%s+$", ""))
    end, {"-c", cmd}):start()
end

-- ── Tab target ────────────────────────────────────────────────────────────────

-- Read the focused session from the portal's shadow file; fall back to default.
local function readActiveSession()
    local f = io.open(ACTIVE_FILE, "r")
    if not f then return DEFAULT_TARGET end
    local s = f:read("*l")   -- first line only
    f:close()
    if s then s = s:gsub("^%s+", ""):gsub("%s+$", "") end
    if s and s ~= "" then return s end
    return DEFAULT_TARGET
end

-- ── Safety auto-stop ──────────────────────────────────────────────────────────

local function clearSafety()
    if safetyTimer then safetyTimer:stop(); safetyTimer = nil end
end

local function armSafety(onTimeout)
    clearSafety()
    safetyTimer = hs.timer.doAfter(SAFETY_SECS, onTimeout)
end

-- ── ⌥Space — talk to the tab target ──────────────────────────────────────────
-- Toggle: tap to start, tap again to stop + send. The target is re-read from
-- the shadow file at STOP time, so it follows whatever tab you focused last.

local function finishTab()
    if mode ~= "tab" then return end   -- idempotent: guard double-fire
    mode = nil
    clearSafety()
    local target = readActiveSession()
    hs.alert.show("→ " .. target, 0.6)
    run({"listen", "stop", "-s", target})
end

local function toggleTab()
    if mode == "tab" then
        finishTab()
    elseif mode == nil then
        mode = "tab"
        hs.alert.show("● Recording → " .. readActiveSession(), 0.6)
        run({"listen", "start"})
        armSafety(finishTab)
    end
    -- if mode == "pick", ignore — the other key owns the capture.
end

-- ── ⌥⌘Space — pick-and-talk ───────────────────────────────────────────────────

-- Fetch live sessions as JSON and hand the array to onSessions(list).
local function fetchSessions(onSessions)
    runCapture({"list", "--sessions", "--json"}, function(stdout)
        local ok, data = pcall(hs.json.decode, stdout)
        if ok and data and data.sessions then
            onSessions(data.sessions)
        else
            onSessions({})
        end
    end)
end

-- Stop the pick capture. `session` non-empty → send there; nil/empty → cancel.
-- Idempotent: the mode guard means the hotkey-again path and the chooser
-- completion callback can't both send.
local function finishPick(session)
    if mode ~= "pick" then return end
    mode = nil
    clearSafety()
    if chooser then chooser:hide(); chooser = nil end
    if session and session ~= "" then
        hs.alert.show("→ " .. session, 0.6)
        run({"listen", "stop", "-s", session})
    else
        hs.alert.show("✕ cancelled", 0.6)
        run({"listen", "cancel"})
    end
end

local function buildChooser(sessions)
    local choices = {}
    for _, s in ipairs(sessions) do
        choices[#choices + 1] = {
            text = s.name,
            subText = (s.type or "session") .. (s.machine and (" @ " .. s.machine) or ""),
            session = s.name,
        }
    end
    -- Completion fires on Enter / click (a choice) or Esc / dismiss (nil).
    local c = hs.chooser.new(function(choice)
        finishPick(choice and choice.session or nil)
    end)
    c:choices(choices)
    c:placeholderText("🎤 Talk + pick a session…")
    return c
end

local function startPick()
    mode = "pick"
    run({"listen", "start"})   -- record while you pick
    fetchSessions(function(sessions)
        if mode ~= "pick" then return end   -- already finished/cancelled
        if #sessions == 0 then
            hs.alert.show("No sessions running")
            finishPick(nil)
            return
        end
        chooser = buildChooser(sessions)
        chooser:show()
    end)
    armSafety(function() finishPick(nil) end)   -- timeout cancels (never misroute)
end

local function togglePick()
    if mode == "pick" then
        -- Second press: send to the currently highlighted row.
        -- selectedRowContents() returns the visible selected row's table.
        local row = chooser and chooser:selectedRowContents()
        finishPick(row and row.session or nil)
    elseif mode == nil then
        startPick()
    end
    -- if mode == "tab", ignore — the other key owns the capture.
end

-- ── Hotkeys ───────────────────────────────────────────────────────────────────
hs.hotkey.bind({"alt"}, "space", toggleTab)          -- ⌥Space  — talk to tab target
hs.hotkey.bind({"alt", "cmd"}, "space", togglePick)  -- ⌥⌘Space — pick-and-talk

hs.alert.show("AgentWire PTT ready — ⌥Space talk · ⌥⌘Space pick+talk")
