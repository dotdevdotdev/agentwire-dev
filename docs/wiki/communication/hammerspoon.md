# Hammerspoon Push-to-Talk

Global hotkeys for voice input on macOS using [Hammerspoon](https://www.hammerspoon.org/). Hold a key to record, release to send — works from any app.

## Prerequisites

1. **Hammerspoon** installed (`brew install --cask hammerspoon`)
2. **AgentWire** installed and on PATH (`~/.local/bin/agentwire`)
3. **STT server** running (`agentwire stt start`)
4. **IPC module** — Hammerspoon needs `hs.ipc` for CLI integration. On first load it may prompt to install the CLI.

## Setup

Copy this to `~/.hammerspoon/init.lua` and reload (Hammerspoon menu > Reload Config or `Cmd+Shift+R`).

```lua
-- Enable IPC for hs CLI commands
require("hs.ipc")

-- AgentWire Push-to-Talk
-- Hold right alt to record, release to send to target session
-- Hold right cmd to record, release to type at cursor
-- Alt+F1 to change target session

local recording = false
local typeMode = false
local targetSession = "agentwire-dev"  -- Default session, resets on reload
local agentwire = os.getenv("HOME") .. "/.local/bin/agentwire"
local modTap = nil  -- Forward declaration

-- Non-blocking command execution, with optional callback when done
local function run(args, onComplete)
    local cmd = agentwire .. " " .. table.concat(args, " ")
    hs.task.new("/bin/bash", function()
        if onComplete then onComplete() end
    end, {"-c", cmd}):start()
end

-- Run command with eventtap disabled until complete
local function runAndWait(args)
    modTap:stop()
    run(args, function()
        modTap:start()
    end)
end

-- Watch for modifier key changes
modTap = hs.eventtap.new({hs.eventtap.event.types.flagsChanged}, function(event)
    local flags = event:getFlags()
    local keyCode = event:getKeyCode()

    -- Right alt is keyCode 61 (send to session)
    if keyCode == 61 then
        if flags.alt and not recording then
            recording = true
            typeMode = false
            hs.alert.show("Recording → " .. targetSession, 0.5)
            run({"listen", "start"})
        elseif not flags.alt and recording and not typeMode then
            recording = false
            hs.alert.show("Sending to " .. targetSession, 0.5)
            runAndWait({"listen", "stop", "-s", targetSession})
        end
    end

    -- Right cmd is keyCode 54 (type at cursor)
    if keyCode == 54 then
        if flags.cmd and not recording then
            recording = true
            typeMode = true
            hs.alert.show("Recording (type)...", 0.5)
            run({"listen", "start"})
        elseif not flags.cmd and recording and typeMode then
            recording = false
            typeMode = false
            hs.alert.show("Typing...", 0.5)
            runAndWait({"listen", "stop", "--type"})
        end
    end

    return false
end)

-- Alt+F1 to change target session
hs.hotkey.bind({"alt"}, "F1", function()
    if recording then
        recording = false
        typeMode = false
    end

    local button, text = hs.dialog.textPrompt(
        "AgentWire Target Session",
        "Enter session name:",
        targetSession,
        "OK",
        "Cancel"
    )

    if button == "OK" and text and text ~= "" then
        targetSession = text
        hs.alert.show("Target: " .. targetSession, 1)
    end
end)

modTap:start()
hs.alert.show("PTT: " .. targetSession .. " (⌥F1 to change)")
```

## Hotkeys

| Key | Action |
|-----|--------|
| **Hold Right Alt** | Record voice, release sends to target session |
| **Hold Right Cmd** | Record voice, release types transcription at cursor |
| **Alt + F1** | Change target session (text prompt) |

## How It Works

### Two modes

**Session send** (Right Alt) — transcribes your voice and sends the text as a prompt to the target agentwire session. The agent receives it as if you typed it in the terminal.

**Type at cursor** (Right Cmd) — transcribes your voice and types the text wherever your cursor is. Works in any app (editor, browser, Slack, etc.). Useful for dictation outside of agent sessions.

### Key mechanics

- **Hold to record, release to send** — no toggle, no button. Natural push-to-talk.
- **Modifier keys only** — uses `flagsChanged` eventtap to detect right alt/cmd press and release. No conflict with normal keyboard shortcuts since left modifiers are unaffected.
- **Non-blocking** — recording starts and stops via `hs.task` (async). The eventtap is disabled during send to prevent double-fires, then re-enabled on completion.

### Session targeting

The default target session is set at the top of the config (`targetSession`). Press **Alt+F1** to change it at any time — a dialog prompts for the session name. Reloading the config resets to the default.

Session names match `agentwire list` output (e.g., `agentwire-dev`, `myproject`, `myproject/feature-branch`).

## Customization

### Change default session

```lua
local targetSession = "my-project"  -- Change this
```

### Change hotkeys

Modifier key codes for `flagsChanged` events:

| Key | Code |
|-----|------|
| Right Alt | 61 |
| Right Cmd | 54 |
| Right Shift | 60 |
| Right Ctrl | 62 |

To use Right Shift instead of Right Alt for session send, change `keyCode == 61` to `keyCode == 60`.

For the session selector, change the `hs.hotkey.bind` call:

```lua
-- Use Ctrl+F1 instead of Alt+F1
hs.hotkey.bind({"ctrl"}, "F1", function()
```

### Change agentwire path

If your `agentwire` binary is elsewhere:

```lua
local agentwire = "/usr/local/bin/agentwire"
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No alert on key press | Check Hammerspoon is running and config is loaded. Console: `hs.alert.show("test")` |
| "Recording" shows but no transcription | Check STT server: `agentwire stt status` |
| Transcription sent but agent doesn't respond | Check session exists: `agentwire list` |
| Right alt/cmd conflicts with other apps | Most apps only bind left modifiers. If conflict exists, remap to Right Shift (code 60) |
| Double-fire on release | The `runAndWait` guard should prevent this. If it happens, check Hammerspoon console for errors |
