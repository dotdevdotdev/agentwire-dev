> Living document. Update this, don't create new versions.

# Mission: Onboarding Redesign

**Issue:** [#63](https://github.com/dotdevdotdev/agentwire/issues/63)
**Branch:** `63-onboarding-redesign`
**Status:** Implementation Complete (needs testing)

## Goal

Redesign onboarding to ask fewer questions upfront (3 max) and let Claude handle complex setup interactively.

## Current State

`agentwire/onboarding.py` (~1000 lines) has a comprehensive wizard that:
- Runs pre-flight checks (Python, tmux, ffmpeg, agents)
- Handles existing config detection/migration
- Asks 15+ questions across 7 sections
- Uses Python `input()` which has issues (no backspace in some contexts)

### Current Flow
1. Pre-flight checks (Python, tmux, ffmpeg, agents)
2. Existing config detection
3. Projects directory
4. Agent selection
5. Network topology (standalone/multi-machine)
6. TTS backend configuration
7. STT configuration
8. SSL certificates
9. Remote machines (if multi-machine)
10. Final validation

## Proposed Flow

### Phase 1: Minimal Questions (Python wizard)
```
1. Projects directory [~/projects]:
2. Which agent? [Claude Code / OpenCode]
3. Setup type? [Standalone / Multi-machine]
```

Write minimal `config.yaml`, then spawn Claude session.

### Phase 2: Claude-Assisted Setup
Claude handles interactively:
- TTS configuration (backend, server, voice)
- STT configuration (server URL, model selection)
- SSL certificates (generate or skip)
- Remote machines (add, test, tunnels)
- Validation (test each service)

### Non-Interactive Path
Keep `agentwire init --non-interactive` with:
- Fix readline support
- Accept config via flags: `--projects ~/code --agent claude --standalone`

## Tasks

- [x] Investigate: Map current wizard sections to what Claude should handle
- [x] Investigate: Review init.md prompt for Claude setup guidance
- [x] Design: Create minimal pre-question flow spec
- [x] Design: Write Claude setup prompt/role
- [x] Implement: Refactor onboarding.py for minimal flow
- [x] Implement: Create init role for Claude-assisted setup
- [x] Fix parameter mismatch bug (`skip_agentwire` → `skip_session`)
- [ ] Test: Both paths end-to-end
- [ ] Implement: Add `--non-interactive` with proper flags (future)

## Files to Modify

| File | Changes |
|------|---------|
| `agentwire/onboarding.py` | Simplify to 3 questions, spawn Claude |
| `agentwire/__main__.py` | Add `--non-interactive` flags |
| `agentwire/roles/init.md` | New role for Claude-assisted setup |
| `agentwire/prompts/init.md` | Setup prompt if needed |

## Key Decisions Needed

1. **What stays in Python wizard?**
   - Pre-flight checks (Python, tmux) - probably yes
   - Projects dir, agent, topology - yes
   - Everything else → Claude

2. **How does Claude session get spawned?**
   - `agentwire new -s init-setup --roles init` after minimal config written?
   - Or inline in the wizard?

3. **Error handling in Claude phase?**
   - Claude can re-ask, validate, recover
   - What if user exits mid-setup?

## Notes

### Implementation Summary (2026-02-03)

**Changes made:**

1. **`agentwire/onboarding.py`** - Completely rewritten from ~1580 lines to ~435 lines
   - Asks only 3 questions: projects dir, agent choice, topology
   - Pre-flight checks remain (Python, tmux, ffmpeg, agents)
   - Writes minimal config.yaml with TTS/STT disabled
   - Spawns `agentwire-init` session with `init` role for interactive setup

2. **`agentwire/roles/init.md`** - New role for Claude-assisted setup
   - Guides user through TTS, STT, SSL, and remote machines configuration
   - Conversational style, explains what each service does
   - Tests each service after configuration

3. **`agentwire/__main__.py`** - Fixed parameter mismatch bug
   - Changed `skip_agentwire=True` to `skip_session=True` at line 5077

### Parameter Mismatch Bug (Issue #62) - FIXED
`cmd_init` was passing `skip_agentwire=True` but `run_onboarding` expected `skip_session`. Fixed.
