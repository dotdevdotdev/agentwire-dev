-- AgentWire — toggle push-to-talk + voice target-picker (Hammerspoon)
-- ============================================================================
-- Drop this in ~/.hammerspoon/init.lua (or `require` it from yours) and reload
-- (Hammerspoon menu > Reload Config, or ⌘⇧R).
--
-- What you get:
--   • Toggle PTT — tap a hotkey to start recording, tap again to stop and
--     send the transcript to the current target session.
--   • Toggle dictation — same, but types the transcript at your cursor.
--   • Voice target-picker — say a session name; the script transcribes it,
--     fuzzy-matches against your live sessions, and auto-confirms the winner.
--
-- It shells out to the agentwire CLI:
--   agentwire listen start                 -- begin recording (async)
--   agentwire listen stop -s <session>     -- stop, transcribe, send to session
--   agentwire listen stop --type           -- stop, transcribe, type at cursor
--   agentwire listen stop --stdout         -- stop, transcribe, print transcript
--                                             to stdout (no paste, no tmux send)
--   agentwire list --sessions --json       -- live session list, as JSON
--
-- The `--stdout` form is the key piece: it hands the raw transcript back to
-- this script so we can fuzzy-match it ourselves instead of sending it
-- anywhere. Requires `agentwire stt start` running with stt.backend: custom.
-- ============================================================================

require("hs.ipc")  -- needed for the `hs` CLI; first load may prompt to install

-- ── Config ──────────────────────────────────────────────────────────────────
-- A roomy PATH so the CLI's child processes (ffmpeg, etc.) resolve when
-- Hammerspoon launches them outside your login shell.
local PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
local agentwire = os.getenv("HOME") .. "/.local/bin/agentwire"

local HYPER = {"ctrl", "alt", "cmd"}  -- conflict-free chord for our hotkeys
local MATCH_THRESHOLD = 0.45          -- min fuzzy score to auto-confirm a pick

local targetSession = "agentwire"     -- default target; resets on config reload

-- ── State ─────────────────────────────────────────────────────────────────
local recording = false   -- a PTT/dictation capture is in flight
local modTap              -- forward decl (kept for symmetry / future eventtaps)

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

-- ── Fuzzy matching ───────────────────────────────────────────────────────────
-- STT mangles short, jargon-y session names ("agentwire-dev" → "agent wire dev",
-- "agent wired f"). A plain equality or prefix test misses those, so we score
-- character-level similarity (Levenshtein) and add a bonus for each spoken word
-- that's contained in the candidate. Highest combined score wins.

-- Character-level edit distance. Two rolling rows, O(min) memory.
local function levenshtein(a, b)
    local la, lb = #a, #b
    if la == 0 then return lb end
    if lb == 0 then return la end
    local prev, cur = {}, {}
    for j = 0, lb do prev[j] = j end
    for i = 1, la do
        cur[0] = i
        local ca = a:byte(i)
        for j = 1, lb do
            local cost = (ca == b:byte(j)) and 0 or 1
            cur[j] = math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        end
        prev, cur = cur, prev   -- swap; the finished row is now in `prev`
    end
    return prev[lb]
end

-- 0..1 char-level similarity, plus +0.5 per spoken word found in the candidate.
local function matchScore(query, candidate)
    query, candidate = query:lower(), candidate:lower()
    local dist = levenshtein(query, candidate)
    local maxLen = math.max(#query, #candidate, 1)
    local sim = 1 - (dist / maxLen)

    local bonus = 0
    for word in query:gmatch("%S+") do
        if #word >= 2 and candidate:find(word, 1, true) then
            bonus = bonus + 0.5
        end
    end
    return sim + bonus
end

-- ── Voice target-picker ───────────────────────────────────────────────────────

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

-- Build a populated chooser and auto-confirm the best fuzzy match for `heard`.
local function showPicker(heard, sessions)
    if #sessions == 0 then
        hs.alert.show("No sessions running")
        return
    end

    -- GOTCHA #1 — hs.chooser:choices() is a SETTER, not a getter.
    -- Calling chooser:choices() with no args *clears* the list. So we keep our
    -- own `choices` table and read fuzzy candidates from it, never from the
    -- chooser. We only ever pass `choices` *into* chooser:choices(choices).
    local choices = {}
    for _, s in ipairs(sessions) do
        choices[#choices + 1] = {
            text = s.name,
            subText = (s.type or "session") .. (s.machine and (" @ " .. s.machine) or ""),
            session = s.name,
        }
    end

    local chooser = hs.chooser.new(function(choice)
        -- nil when the user pressed Esc / clicked away.
        if choice then
            targetSession = choice.session
            hs.alert.show("🎯 Target: " .. targetSession, 1)
        end
    end)
    chooser:choices(choices)                  -- setter call: hand it the list
    chooser:placeholderText("Heard: " .. heard)

    -- Score every row against our own `choices` var (see GOTCHA #1).
    local bestRow, bestScore = nil, -1
    for i, c in ipairs(choices) do
        local sc = matchScore(heard, c.text)
        if sc > bestScore then
            bestRow, bestScore = i, sc
        end
    end

    chooser:show()

    -- GOTCHA #2 — chooser:select(row) fires the completion callback AND closes
    -- the chooser. That's exactly what we want for auto-confirm: select the
    -- fuzzy winner and the callback above sets targetSession, no click needed.
    -- Below the confidence threshold we leave the chooser open so the human
    -- can pick (or type to filter) instead.
    if bestRow and bestScore >= MATCH_THRESHOLD then
        chooser:select(bestRow)
    end
end

-- Toggle: first tap records, second tap stops + transcribes + opens the picker.
local pickerArmed = false
local function voicePickTarget()
    if not pickerArmed then
        pickerArmed = true
        hs.alert.show("🎯 Say a session name…", 0.6)
        run({"listen", "start"})
    else
        pickerArmed = false
        runCapture({"listen", "stop", "--stdout"}, function(transcript)
            if transcript == "" then
                hs.alert.show("No speech detected")
                return
            end
            fetchSessions(function(sessions)
                showPicker(transcript, sessions)
            end)
        end)
    end
end

-- ── Toggle PTT (send) and dictation (type) ───────────────────────────────────
-- Tap to start, tap the SAME key to stop. `recording` guards both so a stray
-- second hotkey can't double-fire `listen start`.

local function toggleSend()
    if recording then
        recording = false
        hs.alert.show("→ " .. targetSession, 0.6)
        run({"listen", "stop", "-s", targetSession})
    else
        recording = true
        hs.alert.show("● Recording → " .. targetSession, 0.6)
        run({"listen", "start"})
    end
end

local function toggleType()
    if recording then
        recording = false
        hs.alert.show("⌨︎ Typing…", 0.6)
        run({"listen", "stop", "--type"})
    else
        recording = true
        hs.alert.show("● Recording (dictate)…", 0.6)
        run({"listen", "start"})
    end
end

-- ── Hotkeys ───────────────────────────────────────────────────────────────────
hs.hotkey.bind(HYPER, "space", toggleSend)        -- ⌃⌥⌘Space — record → send
hs.hotkey.bind(HYPER, "t", toggleType)            -- ⌃⌥⌘T     — record → type
hs.hotkey.bind(HYPER, "s", voicePickTarget)       -- ⌃⌥⌘S     — pick target by voice

hs.alert.show("AgentWire PTT ready → " .. targetSession .. "  (⌃⌥⌘S to retarget)")
