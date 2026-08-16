# CLAUDE.md — hibrit

Working rules for this repository.

## What this is

hibrit moves Dolby Vision RPUs and HDR10+ metadata from one release of a film
into another, wrapping `dovi_tool` and `hdr10plus_tool`. It never re-encodes.

Read `README.md` first — it explains the trap the whole design is built around
(`dovi_tool inject-rpu` pads a mismatched RPU and exits 0).

## Language

**Everything in this repository is English**: code, comments, docstrings, CLI
output, GUI labels, README, commit messages, issues. The Turkish explanation of
the same subject lives in the author's notes, not here.

## The rule that matters

**A tool that measures must be able to say no.**

Anything that produces a number also produces a verdict, and the verdict may be
a refusal. Applied concretely:

- `align()` returns `Alignment`, not `int`. `no_match` is a legitimate answer.
- `DoviTool.inject_rpu()` raises on a frame-count mismatch and **deletes the
  file it wrote**. A misaligned output is worse than no output, because it looks
  finished.
- `build_plan()` returns blockers alongside steps, and `Plan.ok` is false when
  any blocker is present.
- `verify.Report` distinguishes *passed* from *not measured*. A skipped check is
  never reported as a pass.

Before adding anything that returns a bare number, ask what it does when the
inputs are wrong.

## Thresholds scale with the material

There is a four-order-of-magnitude gap between a forty-second test clip and a
three-hour remux. Search ranges, window sizes and gap tolerances are fractions
of the runtime, never fixed frame counts. `window_plan()` is where this lives;
keep new thresholds there rather than scattering constants.

## Disk

Assume the target is 70 GB and the drive it lives on has 40 GB free.

- Never write next to the source files. `--workdir` is required, not defaulted.
- Check free space before the first command, not after the third.
- Delete each intermediate as soon as the next step has consumed it.
- The one exception is the extracted target stream, kept for the pixel check and
  deleted by the caller.

## Tests

Three tiers, and the split is deliberate:

| Command | Needs | Proves |
|---|---|---|
| `pytest -q -m "not tools and not real"` | nothing | the code does what it says |
| `pytest -q -m tools` | the binaries | the wrappers drive them correctly |
| `pytest -q -m real` | real clips (`HIBRIT_MEDIA`) | the result is right |
| `pytest -q -m gui` | a display | the window is wired to the core |

The window tests are out of the default run because a machine without a display
would report them as failures rather than as something it could not attempt.
They drive the message queue by hand rather than joining the worker thread: a
test that waited on the thread would pass even if the queue wiring were broken,
and the wiring is the part that has actually been wrong.

CI runs the first two. The `tools` tier synthesises everything it needs —
`dovi_tool generate` writes an RPU with no video involved, ffmpeg builds an HDR10
clip in a fifth of a second — so keep it that way: a `tools` test that reaches
for a file on disk belongs in `real`.

When adding a feature that touches metadata, add a test where the correct answer
is known in advance, and build the input by a route the code under test does not
use.

**Always test the refusal.** For every test that feeds a matching pair, add one
that feeds a mismatched pair and asserts the tool declines. A suite that only
ever sees matching inputs never observes the case the user will actually hit.

## Style

- Docstrings say *why*, not *what*. The commands are short; the reasons are not.
- A comment that records a measurement beats one that records an intention.
  "measured: both orders produce byte-identical output" is worth keeping;
  "inject HDR10+ first" is not.
- `ruff check .` and `ruff format .` before committing. Line length 100.
- Type hints everywhere. `from __future__ import annotations` at the top.
- Frozen dataclasses for anything that crosses a module boundary.

## Do not

- Bundle `dovi_tool` or `hdr10plus_tool`. They are separate GPL programs under
  active development; `hibrit doctor` names what is missing instead.
- Install anything system-wide, or write outside the working directory.
- Add a dependency without a reason that survives being written down. The only
  runtime dependency is numpy, for the FFT.
- Widen a tolerance to make a test pass.
