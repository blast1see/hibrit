# Changelog

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

Both files are decoded to 128×72 grayscale — about 250 fps for 4K HEVC — and
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

### Disk

A target needs three times its size in working space — traced on a 72 GB remux,
where three streams coexist before the remux and the peak comes to about 208 GB.
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

### Tests

Four tiers. The first needs nothing installed. The second drives the real
binaries against material it synthesises — `dovi_tool generate` writes an RPU
with no video involved — and both of those run in CI, on Linux and Windows. The
third needs real clips and is the one that can fail in the most interesting way.
The fourth drives the window, and needs a display.

About half of the tests in the upper two tiers feed inputs that should be
refused: a shortened RPU, a lengthened one, two unrelated films, a search range
too narrow to contain the answer.
