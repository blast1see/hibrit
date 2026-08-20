# Changelog

## 0.1.3

`hibrit plot -o` told the packaged build's user to `pip install`, which is
advice they cannot follow: the zip has no Python in it. It now says what is
actually true there -- comparing two files works without matplotlib, so drop
the `-o` -- and keeps the pip line for source installs, where it works.

Caught by running the published zip rather than the source tree.

## 0.1.2

### `hibrit plot`

Reads the L1 dynamic brightness metadata out of an RPU, and compares two files
frame for frame. Releases cut from the same master carry identical L1 curves,
so where two curves diverge is where the releases diverge -- which is the
question frame alignment turns on, answered without decoding a single frame.

Measured on a real pair: a UHD remux and a WEB-DL of the same film agree on
every one of 3000 frames except the first 24, and frame 24 is a scene cut.
Their openings differ; the rest is the same master. An offset measured from
the opening of that film is wrong, and nothing in the file says so.

With `-o` it draws the curve as well. That needs matplotlib, installed with
`pip install 'hibrit[plot]'` rather than by default -- bundling it would
roughly double the packaged build for a command most people never run.


## 0.1.1

`hibrit doctor` ends by telling you to put the missing binaries in
`hibrit/tools/`, and the 0.1.0 build did not contain that folder. Unzipping it
and following the advice meant creating a directory and hoping it was the right
one. It now ships, with a note inside naming the two tools and where to get
them.

Found by downloading the published 0.1.0 archive and using it. Every test until
then had run against a source checkout, where `tools/` already exists — so the
gap could only appear to someone starting from the zip.

## 0.1.0

First release.

### Moving the metadata

Point it at two releases of the same film — one with the Dolby Vision or HDR10+
metadata, one with the picture you want to keep — and it transfers the metadata
without re-encoding a frame. Profile 5 and profile 7 sources are converted to
single-layer 8.1 along the way; profile 8.1 passes through unchanged.

`hibrit plan` shows what would happen and what would be lost without touching a
byte. It refuses jobs that cannot work: a target that is not HEVC, a resolution
or frame rate mismatch, or two files whose runtimes differ by more than a trimmed
logo could account for.

### The refusal this is built around

`dovi_tool inject-rpu` does not fail when the RPU's frame count disagrees with
the video. It prints a warning, pads the metadata to length, and exits 0. The
file plays, players show a Dolby Vision badge, and every frame after the
mismatch carries metadata belonging to a different one.

Two releases of the same film almost never share a frame count, so this is the
normal case rather than an edge case. hibrit refuses the injection, deletes the
half-written file, and says what to do instead.

### Frame alignment

Both files are decoded to 128×72 grayscale — about 180 fps for 4K HEVC — and
reduced to one number per frame, which is cross-correlated to find the offset.

The signal is the clipped absolute frame-to-frame difference, not mean
brightness. Brightness barely moves inside a shot, so its correlation surface is
a plateau and the peak wanders: on a clip built with a known offset of +137 it
answered +135 at a confidence of 1.05. The cut signal answers +137 at 3.24.

Three things keep the number honest. Every result carries a confidence ratio and
`no_match` is a verdict it is willing to reach. The offset is measured in two
places in the film, and windows that disagree produce a refusal rather than an
average. A peak sitting on the wall of the search range is reported as a search
that was too narrow, not as an answer.

Thresholds scale with the material rather than being fixed frame counts: a
three-hour remux and a forty-second clip are four orders of magnitude apart.

What it returns is **one** offset, and two releases do not always differ by one.
Measured on a real pair: -26 frames for the film and -24 for the first 38
seconds, where two frames are dropped at the handover out of the opening titles.
The reported number is right for 99.5% of the runtime and wrong for the opening.
The two-window rule does not catch that and is not meant to — a splice affecting
five hundred frames out of a hundred and eighty thousand is a minority inside
any window wide enough to correlate.

### When only part of the job is possible

A streaming release is usually cropped to its own picture while the disc keeps
the bars inside the frame — 3840x1598 against 3840x2160 for the same film. The
mismatch means different things depending on what is moving, so the refusal says
which one applies:

* the **RPU** cannot move at all. Level 5 offsets are pixel counts in the source
  frame, and a 2160-row offset points at rows a 1598-row frame does not have.
* **HDR10+** can, with `--allow-crop`. Its per-frame values are measurements of
  the frame the encoder saw, which is a different set of pixels rather than
  misplaced geometry — something a person can knowingly accept. The warning says
  plainly that the transfer will finish and every check will pass.

`--only dv` / `--only hdr10plus` limits the transfer to one kind, and the
refusal names it: when the RPU is what blocks the job and something else was
moving too, the message says which flag gets the rest through. The window has
the same controls, because a plan that prints advice the interface cannot act on
is a dead end.

### Verification

The output is checked against the inputs it was built from. The RPU and HDR10+
metadata are read back out and compared, the frame counts are compared, and the
coded picture data of the result is hashed against the original target's.

That last check hashes only VCL NAL units. Hashing whole files does not work:
`dovi_tool inject-rpu` adds a seven-byte access unit delimiter to every frame
that lacks one and `dovi_tool remove` does not take them back out, so a
whole-file comparison passes on Blu-ray remuxes — which already carry them — and
fails on a clip straight out of ffmpeg. Restricting the hash to picture data
answers the question exactly and costs a read rather than a rewrite, so it runs
by default.

Two full films have been through this end to end. The stronger result is the one
this project had no hand in: for *The Prestige* there is a hybrid remux made
independently by someone else, and taking the same WEB-DL, aligning it with the
offset align() measured and retiming the metadata produces a file with the same
SHA-256 as the HDR10+ already inside that release — 188,047,925 bytes, 187,467
frames. Running the transfer for real then produced a file equivalent to it: the
same picture bit for bit and the same metadata, differing only in which audio and
subtitle tracks it keeps, and there hibrit keeps the target's.

### Disk

A target needs three times its size in working space — measured on a 72.8 GiB
remux, where the peak comes to 211 GiB. It arrives twice by different routes:
three streams coexist before the remux, and verification later holds the
extracted target, the finished output and an extraction taken from it.
`--workdir` is required rather than defaulted, because the drive holding the
sources is usually the one without the room. Free space is checked before the
first command runs, and each intermediate is deleted as soon as the next step
has consumed it.

Every step that rewrites the stream takes minutes at that size, so the tools'
own progress is forwarded as it arrives — thinned to ten-percent steps, since
raw it is four hundred lines of counting.

### The window

`hibrit gui`. Same core as the command line. The measured offset, its confidence
and the number of places it was measured are printed in one line at one size,
because they are one fact; an untrustworthy result cannot be used without
ticking a box that says so in words.

### The build

The Windows package was built and run before this was written: `hibrit doctor`
finds the binaries in the `tools/` folder beside the executable, a full job
runs through it with every verification check passing, and both executables
open. A fresh unzip has an empty `tools/`, so doctor reports dovi_tool and
hdr10plus_tool as missing and prints where to download them.

### Tests

Four tiers. The first needs nothing installed. The second drives the real
binaries against material it synthesises — `dovi_tool generate` writes an RPU
with no video involved — and both of those run in CI, on Linux and Windows. The
third needs real clips and is the one that can fail in the most interesting way.
The fourth drives the window, and needs a display.

About half of the tests in the upper two tiers feed inputs that should be
refused: a shortened RPU, a lengthened one, two unrelated films, a search range
too narrow to contain the answer.

Where a test can hand its result to the tool that has to accept it, it does. A
retiming config can be arithmetically right and still be rejected — `remove`
entries apply in sequence while `duplicate` entries apply together, so a prepend
does not move the index of the last frame — and that only shows up when the
generated config goes through the real editor.
