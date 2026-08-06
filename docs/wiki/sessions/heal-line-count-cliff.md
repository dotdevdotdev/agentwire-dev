# The #689 heal cliff — two wedge regimes (#930)

> A measured property of the drain's `stuck` test in `inbox.flush_session` and
> the [#689 heal](messaging.md#how-it-works). Every long or coalesced message in
> the system is subject to it — this is not a property of any one sender.

The heal that closes the paste-lands-but-Enter-is-swallowed hole works by
finding a pending message's own rendered text inside the recipient's input box
and pressing Enter only (`inbox.flush_session` → `session_ready.finish_submit`).
The test is whitespace-normalized substring containment: the message's render
must appear in the captured box content. **When the box does not render the
whole paste, that test misses, and the message is never healed.**

There are two distinct ways the box stops rendering the whole paste, and they
are governed by *different variables*. The numbers below were measured while
building an unrelated voice spike; they are written up here because this is
where they belong, and the fix is owned by
[#930](https://github.com/dotdevdotdev/agentwire-dev/issues/930).

## The wrong model, and who held it

The intuitive model is *chip versus text*: a paste either renders as
`[Pasted text #N +M lines]` (heal fails) or as text (heal works).

**That model is wrong, and it survived two independent sessions whose job was to
be skeptical.** An orchestrator and its adversarial reviewer — a session whose
entire assignment was to catch exactly this kind of unmeasured claim — both
worked from it, and the reviewer asserted it in a written finding. The
reviewer's own summary afterwards:

> listing a failure mode without ranking or measuring it is not much better than
> missing it — I gave you a boundary claim dressed as measured when only the
> direction was.

Keep that in mind when reading the tables: the box **windows long before it
chips**, and a paste that is visibly still text can already be unhealable.

## Regime 1 — a single long line WINDOWS (measured, 80x24)

Character-governed. No chip appears at all; the box simply renders a window onto
a longer body, and containment fails against the part it isn't showing.

| Rendered line | Box holds | `stuck` test |
|---|---|---|
| 430 | 440 | hit ✓ |
| 520 | 532 | hit ✓ — last passing |
| **530** | **467** | **miss** — first measured failure; the box renders only a window |
| 540 | 480 | **miss** — still windowing |
| 880 | 16 | **miss** — now a chip |

So `stuck` fails from **530** chars — bracketed to `(520, 530]`, not estimated —
a full ~350 chars *before* the chip appears. **"It isn't a chip" is not evidence
the heal will fire.**

## Regime 2 — FOUR OR MORE LINES chip, at any size (measured)

Line count, not character count, is the trigger:

| Lines | Chars | Box holds | Chip |
|---|---|---|---|
| 2 | 43 | 45 | no |
| 3 | 65 | 69 | no |
| **4** | **87** | **25** | **yes** |
| 6 | 131 | 25 | yes |

The same 87 characters on **one** line renders as text (box 89, no chip). Four
lines chips at 87 characters.

## Two regimes, two different governing variables

It is tempting to summarise the above as "line count, not characters". That is
right about the *chip* and it understates the picture:

| Regime | Governed by | Chip? | `stuck` |
|---|---|---|---|
| Windowing | **characters** (530+ on one line) | **no** | miss |
| Chip | **lines** (4+, at any size) | yes | miss |

The decisive pair, from the measurements above: **530 characters on ONE line
does not chip** (box 467 — it windows), while **515 characters on FOUR lines
does** (box 25). Fewer characters, more lines, chip appears. Both wedge
identically.

**Consequence for #930: a fix that addresses only the 4-line chip cliff leaves
the character-governed windowing wedge open.** Any probe carried by that work
needs rows for both.

*Prediction, explicitly NOT a measurement:* from these numbers, a 3-line blob
over roughly 470 characters should window without ever chipping. Worth a row in
#930's probe; label it a prediction until someone measures it.

## Why that matters: the drain coalesces

`flush_session` joins the whole pending queue into ONE paste with a **newline**
(`"\n".join(m.render() for m in messages)`) and then tests **each** message's
render against that single box content. Measured with real messages:

| Queued | Chars | Box | `stuck` hits |
|---|---|---|---|
| 1 | 128 | 130 | 1/1 |
| 2 | 257 | 263 | 2/2 |
| 3 | 386 | 396 | 3/3 |
| **4** | **515** | **25 (chip)** | **0/4** |

**A drain coalescing four or more messages wedges every one of them** — no
matter how short each is, and with every per-message cap fully respected.

Two consequences worth stating plainly:

- **The variable that governs is the COALESCED length and line count, not the
  message length.** No cap expressed per-message can bound either regime. A
  message that merely happens to be queued behind three others crosses the cliff
  through no fault of its own.
- **The coalesced blob is multi-line by construction**, because the join is a
  newline. So every multi-message drain already has the property single messages
  are careful to avoid, and has since coalescing landed. Combined with a
  swallowed Enter — the condition the #689 heal exists for — the result is a
  **permanent wedge: never healed, never dead-lettered, therefore never
  emailed**, surfacing only via `agentwire doctor`'s stale-pending report after
  two hours. (Mechanically: with no `stuck` match, the drain falls through to
  the unrecognized-box path, and repeated identical content resolves to
  `box_static` — a *no-penalty* defer reason, so `attempts` never reaches
  `MAX_ATTEMPTS` and the dead-letter owner email never fires.)

**Do not read that as rare.** The first version of this note called it rare
because it needs "two intermittent conditions at once" — that was wrong, and
wrong in the same way an over-claim is wrong, just pointed the other way.
Four-plus coalesced messages is **routine on a busy fleet**, not intermittent:
it is the ordinary state of a recipient that has been busy for a minute. So only
one condition is actually intermittent, and **the rate is governed by the
swallowed-Enter path alone**. What makes it unreported is that it is silent, not
that it is uncommon.

## Caveats on the numbers

Measured at **80x24**. The box shows a bounded number of ROWS, so **a shorter
pane windows sooner**. Treat these as an upper bound for that geometry, not as
constants. A number quoted without its pane geometry is misleading.

## Method — and why a live probe rather than a fixture

The numbers come from `tools/voice_heal_probe.py`, which creates its own
throwaway **80x24** tmux session running Claude Code, pastes real rendered
messages into it, leaves the Enter unsent, captures what the input box actually
holds, and then runs the actual heal against it. **A number without the method
is a number the next person will distrust and re-derive**, so the method is
recorded here alongside the results.

The probe itself is **not on `main`**, and not merely because of where it was
written. It imports `agentwire.voice_layer` to render its message bodies and
pins the spike worktree by absolute path in its PEP-723 header, so it does not
*run* on `main` at all. Porting it means rewriting it sender-agnostic — that is
#930 work, not a copy.

That is not the contradiction it looks like against this page's own "a number
without the method gets re-derived". The method above is specified in enough
detail to be rebuilt from — own throwaway 80x24 session, real rendered messages,
Enter left unsent, capture the box, run the actual heal — so what #930 inherits
is a **written spec for the replacement**, not a hole. It needs rows for both
regimes.

Why a live probe and not a unit fixture: the probe **failed on its first run**
for a reason a fixture structurally cannot produce. It read the box too early
and measured a partially-rendered paste — 38 chars for a 159-char body. **A
fixture is fully rendered by construction, so it can never show you that.** That
is the argument for the probe existing.
