# Terminal Window Sizing

> How tmux window sizing works with the portal — what changed in v1.33+, why,
> and how to pick the right policy for your setup.

## TL;DR

The portal used to **force** every tmux window to the browser's exact size on
every resize. As of v1.33+ ([#258](https://github.com/dotdevdotdev/agentwire-dev/issues/258),
[PR #263](https://github.com/dotdevdotdev/agentwire-dev/pull/263)) it no longer
does — the portal is now a polite tmux client that reports its size and lets
**your configured `window-size` policy** decide. Previously-stuck windows heal
automatically the next time you open them in the portal. If you ever wrote
workaround hooks to fight stuck windows, you can delete them.

## What was happening before

Every time you resized the portal's terminal window, the portal ran
`tmux resize-window -x <cols> -y <rows>` behind the scenes. In tmux, an
explicit `-x/-y` resize flips the window into **manual size mode** — the
window gets nailed to those exact dimensions and tmux stops looking at
attached clients entirely. Your `window-size largest` (or `smallest`, or
`latest`) setting was silently ignored from then on, for *every* client:

- Attach a native terminal (iTerm, Alacritty, …) next to the portal and
  resize it — nothing happens. The window stays at whatever the portal
  last set.
- `window-size smallest` ("fit my tablet") never worked.
- The pin was sticky: it survived the portal disconnecting, and even
  un-pinning by hand didn't last, because the next portal resize re-pinned it.

Manual mode is implemented as a window-level `window-size manual` option that
overrides your global/session policy. You can see it on a stuck window with:

```bash
tmux show-options -w -t <session> window-size   # "window-size manual" = stuck
```

## What happens now

1. **Portal resizes are just client resizes.** The portal updates its own
   PTY and notifies tmux (SIGWINCH) — exactly what your native terminal does
   when you drag its corner. tmux then sizes the window per your policy.
2. **Stuck windows self-heal.** When the portal attaches to a session, it
   unsets any window-level `window-size` override. Windows pinned by older
   portal versions snap back to policy-governed sizing the moment you open
   them in the portal.
3. **`agentwire resize` (and the `pane_resize` MCP tool) heal too.** They
   now clear the manual pin and re-fit per policy. (The old implementation
   used `resize-window -A`, which — counterintuitively — resizes once but
   *leaves* manual mode set.)

## Picking your `window-size` policy

Set one of these in `~/.tmux.conf` (then `tmux source-file ~/.tmux.conf`):

| Policy | Behavior | Pick it when |
|---|---|---|
| `largest` | Window sized to the **biggest** attached client; smaller viewers see a cropped view | Your desktop terminal is primary; portal/tablet viewers just peek |
| `smallest` | Window sized to the **smallest** attached client; always fully visible everywhere | You connect from a tablet/phone and want to see the whole window |
| `latest` (tmux default) | Whoever resized last wins | You use one viewer at a time and want it to always fit |

```tmux
set -g window-size smallest
```

Per-session override (e.g. one session should behave differently):

```bash
tmux set -w -t <session> window-size largest
```

## What you might notice after upgrading

- **The portal no longer "always wins."** Under `largest` with a bigger
  native terminal attached, resizing the browser won't reflow the window —
  you'll see tmux's standard smaller-client view (content with padding, or
  cropped). That's your policy working. Want the old "browser drives the
  window" feel? Use `latest`.
- **A window suddenly fits properly on your phone** — that's the heal
  kicking in.

## Cleanup: workaround hooks

If you added hooks like these to fight stuck windows, they're obsolete —
delete them (they're harmless, but they hard-pin a policy at the window
level and will override your global setting):

```tmux
# DELETE these if you have them — pre-v1.33 workarounds
set-hook -g client-attached 'set-option -t "#{session_name}" window-size largest'
set-hook -g after-new-session 'set-option window-size largest'
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Window ignores all client resizes | Still pinned (portal not attached since upgrade) | Open it in the portal once, or run `agentwire resize -s <session>` |
| Window smaller than my terminal | Another (smaller) client is attached under `smallest` | Detach the other client (`tmux detach-client`), or switch policy |
| Browser resize doesn't reflow the window | Bigger client attached under `largest` | Expected — see policy table; use `latest` for last-touch-wins |
| `show-options -w` still says `manual` | Something re-pinned it | Check `~/.tmux.conf` for old workaround hooks (above) |
