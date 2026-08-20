# hibrit

Move Dolby Vision and HDR10+ metadata from one release of a film to another,
without re-encoding a single frame.

*hibrit* is Turkish for "hybrid", after the release naming convention this
produces: `Title.2021.Hybrid.2160p.UHD.BluRay.REMUX.DV.HDR10Plus...`

> **Status: usable, and honest about what it cannot check.** Run end to end on
> a whole film — a 72.8 GiB dual-layer profile 7 UHD remux gaining HDR10+ from
> a WEB-DL of the same title, 209,389 frames, 1h 31m. All five checks passed:
> the HDR10+ read back byte-for-byte identical, the frame count unchanged, the
> existing Dolby Vision untouched, and **all 1,675,112 coded picture units
> identical to the original** — not one bit of picture data moved.
>
> Then checked against work this project had no hand in. For *The Prestige*
> there is a hybrid remux made independently by someone else. Taking the same
> iTunes WEB-DL, aligning it with hibrit's measured offset of -26 frames and
> retiming the metadata produces a file with **the same SHA-256** as the HDR10+
> already inside that release: 188,047,925 bytes, 187,467 frames, 2,625 scenes.
> A stranger reached the same answer, which is the one kind of confirmation a
> program cannot give itself.
>
> Whether the two releases you point it at *should* share metadata is a
> judgement no program can make; see
> [The failure nothing here can catch](#the-failure-nothing-here-can-catch).

---

## Why

A UHD Blu-ray remux has the better picture. A streaming release has the Dolby
Vision metadata. Or the disc has Dolby Vision and the WEB-DL has HDR10+. The
picture and the metadata are in different files, and the metadata is a small
side-channel — a few megabytes of per-frame instructions riding alongside a
70 GB video stream. Moving it changes no pixels.

[`dovi_tool`](https://github.com/quietvoid/dovi_tool) and
[`hdr10plus_tool`](https://github.com/quietvoid/hdr10plus_tool) already do the
moving, and they do it correctly. This project started as a question about
whether they do — and the measured answer is yes, with one specific and serious
exception.

## The trap this is built around

Neither tool stops when the metadata does not fit the video. `dovi_tool
inject-rpu`:

```
Warning: mismatched lengths. video 173802, RPU 5735
Metadata will be duplicated at the end to match video length
```

and `hdr10plus_tool inject`, in the same words with a different noun:

```
Warning: mismatched lengths. video 240, HDR10+ JSON 150
Metadata will be duplicated at the end to match video length
```

Both print that, **exit 0**, and write a finished file. The file plays. Players
show a Dolby Vision badge. Every frame after the mismatch carries metadata
belonging to a different frame, for the entire runtime, and nothing downstream
says so.

Two releases of the same film almost never share a frame count — studio logos,
trimmed credits, a different cut. So this is not an edge case; it is the normal
case. hibrit refuses that injection, deletes the file it was writing, and tells
you to align first:

```
RPU has 800 frames, video has 1000 (200 frames shorter than the video).
dovi_tool would pad or truncate and still produce a file, but the metadata
would be misaligned for the whole runtime. Align the RPU first (hibrit align),
then inject.
```

## Frame alignment

Finding the offset is the part that is actually hard, so it is the part with
the most care in it.

Both files are decoded to 128×72 grayscale — about 200 fps for 4K HEVC — and
reduced to one number per frame. Those two signals are cross-correlated and the
lag is the offset.

Measured on a 223,615-frame UHD remux at the default settings: **four and a half
minutes**. It is the slowest thing hibrit does and the only step worth waiting
on, so it reports each window as it goes.

The signal is **not** mean brightness. Brightness barely moves inside a shot, so
its correlation surface is a plateau and the peak wanders: on a clip built with
a known offset of +137 frames, mean luma answered +135 with a confidence of
1.05, which is not an answer. The frame-accurate landmark in a film is the
**scene cut**. Correlating the clipped absolute frame-to-frame difference
instead — flattening the tallest cuts so a handful of outliers cannot dominate —
gives +137 at confidence 3.24 on the same clip.

Three rules keep the number honest:

* **A correlation always returns a peak.** Every result carries a
  confidence ratio (winning peak ÷ best competing peak), and `no_match` is a
  verdict the tool is willing to reach.
* **One window can lie.** The offset is measured in two places in the film. If
  they disagree, no single offset can align these releases, and the answer is a
  refusal rather than an average.
* **A peak on the wall of the search range is not a peak.** If the best offset
  sits at the edge of what was searched, the real one is probably outside it,
  and that is reported instead of returned.

Every threshold scales with the material rather than being a fixed frame count:
a three-hour remux and a forty-second test clip are four orders of magnitude
apart.

## Verification

`hibrit run` checks its own output, because the alternative is trusting a badge
in a player:

| Check | What it proves |
|---|---|
| Re-extract the RPU, compare to what was injected | the metadata arrived intact, byte for byte |
| Re-extract the HDR10+ JSON, compare | same, for the other layer |
| RPU frame count vs video frame count | nothing was padded behind your back |
| Frame count before vs after | the remux dropped nothing |
| Hash the result's coded picture units and the original's, and compare | **not one bit of picture data changed** |

The last one is the real proof. It hashes only the VCL NAL units — the ones that
carry picture — and ignores every metadata layer and every piece of scaffolding
around them.

It is not free, and the earlier version of this paragraph said it was. The
result is a Matroska file and the hash needs Annex B, so the finished file's
video track is extracted before it can be read: another full copy, about 68 GB
for a 70 GB output. That is budgeted for rather than avoided — verifying
something other than the finished file would not be verification — and the
working-space figure below covers it.

Hashing the whole file instead does not work, and the way it fails is
instructive: `dovi_tool inject-rpu` adds a seven-byte access unit delimiter to
every frame that lacks one, and `dovi_tool remove` does not take them back out.
Blu-ray remuxes already carry those delimiters, so a whole-file comparison passes
there and fails on a clip straight out of ffmpeg — a check that is wrong only on
the material you did not test with.

## Install

Python 3.10 or newer.

```
git clone https://github.com/blast1see/hibrit
cd hibrit
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

Then put four binaries where hibrit can find them — either in `tools/` next to
the package, or anywhere on `PATH`. Nothing is installed system-wide.

| Tool | Source |
|---|---|
| `dovi_tool` | https://github.com/quietvoid/dovi_tool/releases |
| `hdr10plus_tool` | https://github.com/quietvoid/hdr10plus_tool/releases |
| `mediainfo` (CLI) | https://mediaarea.net/en/MediaInfo/Download |
| `ffmpeg`, `mkvmerge`, `mkvextract` | your usual builds |

```
hibrit doctor
```

## Use

```bash
# What does this file carry?
hibrit probe "Dune.Part.One.2021.Hybrid.2160p.UHD.BluRay.REMUX.DV.HDR10Plus.mkv"

# What would happen, and what would be lost? Touches nothing.
hibrit plan web-dl.mkv bluray-remux.mkv

# How far apart are they, and how much do we believe that?
hibrit align web-dl.mkv bluray-remux.mkv

# Do it.
hibrit run web-dl.mkv bluray-remux.mkv -o hybrid.mkv --workdir E:\work
```

`source` is the file that **has** the metadata. `target` is the file whose
**picture** you want to keep. The output is the target's video with the source's
metadata.

There is a window if you prefer one:

```
hibrit gui
```

## Disk

A target needs its video stream extracted, injected into, and remuxed, and when
both kinds of metadata move there are three streams on disk at once before the
remux joins them. Measured on a 72.8 GiB remux, the peak comes to 211 GiB — so
**three times the target's size** is the requirement.

`--workdir` is required rather than defaulted for exactly this reason: the drive
holding your sources is usually the one without the room. Checked against real volumes: a 72 GB job is refused on a drive with 42 GB free
and on one with 193 GB, and accepted on one with room. Free space is
checked before the first command runs, and every intermediate is deleted as soon
as the next step has consumed it — with one deliberate exception, the extracted
target stream, which the picture check needs something to compare against. It is
freed once verification has read it, or immediately if `--no-picture-check` says
that check is not wanted.

The peak arrives twice by different routes and lands in the same place: during
the remux the extracted stream, the injected stream and the growing output
coexist; during verification the extracted stream, the finished output and the
extraction taken from it do. Both measured at 211 GiB on a 72.8 GiB target,
against the 218 GiB that three times over asks for.

## How long it takes

Measured end to end on a whole film: a 72.8 GiB profile 7 dual-layer remux,
209,389 frames, gaining HDR10+ from a WEB-DL of the same title.

| Step | Time | Rate |
|---|---|---|
| Alignment (two windows of 12,558 frames) | 4m 30s | ~183 fps decoded |
| `mkvextract` the target's video track | 12m 00s | 103 MB/s |
| `hdr10plus_tool extract` + retime | 6m 48s | |
| `hdr10plus_tool inject` | 14m 42s | 100 MB/s |
| `mkvmerge` remux | 21m 24s | 60 MB/s |
| Verification, picture check included | 31m 36s | |
| **Total** | **1h 31m** | |

An earlier version of this section measured a 60,000-frame slice at 18.2 GB,
took the 7m 36s that produced, and reckoned a 72 GB remux at "half an hour or
so". The real answer is about an hour for the run and half an hour for the
verification. Scaling by size was the wrong model: the remux pass runs at little
more than half the rate of the extract, and at that point three files the size
of a film are on one disk at once.

Budget an evening rather than a coffee break, and note that the verification is
a third of it — `--no-picture-check` buys most of that back, and gives up the
one check that proves the picture did not move.

## The failure nothing here can catch

Every check above passes on a file that is wrong.

A Dolby Vision RPU contains measurements of the pixels it was authored against:
how bright this shot gets, where the letterbox bars sit, how to roll off the
highlights. If the two releases were graded separately — a different transfer, a
different colourist, a remaster — those numbers describe a picture that is not
in your target. The metadata will be structurally valid, correctly aligned,
byte-for-byte intact, and describing something else.

`hibrit plan` states this assumption every single time, because it is the one
thing the user has to decide and the tool cannot.

### Its everyday cousin: the cropped source

A streaming release is usually cropped to its own picture while the disc keeps
the bars inside the frame — 3840x1606 against 3840x2160 for the same 2.40:1
film. Same title, same edit, different shape, and the mismatch means different
things depending on what is moving:

| Moving | Why it matters | Way through |
|---|---|---|
| Dolby Vision RPU | level 5 offsets point at rows the target frame does not have | none |
| HDR10+ | its per-frame values measure the frame the encoder saw, and a quarter of the target's rows are black bar that was never in it | `--allow-crop` |

The first is misplaced geometry and the second is a measurement taken
elsewhere, which is why one can be knowingly accepted and the other cannot.
`--allow-crop` covers only the second, and says plainly that the transfer will
finish and every check will pass — the metadata stays internally consistent and
only disagrees with the picture it is now attached to.

Rewriting the offsets to the target's geometry is the real fix for the first
case. `dovi_tool editor` does it through an `active_area` config; hibrit does
not, because the correct offsets are a property of the target's bars and this
program does not guess.

## Scope

**Does:** move existing Dolby Vision RPUs and HDR10+ metadata between releases,
convert profile 5 and profile 7 to single-layer 8.1, retime metadata to a
measured offset, remux, and verify.

**Does not:** generate Dolby Vision that does not exist (that is `cm_analyze`
territory and a different problem), re-encode anything, or preserve profile 7's
dual-layer structure as dual-layer.

That last one is a real cost more often than it sounds. A profile 7 enhancement
layer comes in two kinds: a minimal one (MEL) carries no picture correction and
is lost for free, a full one (FEL) carries luma and chroma mapping that
converting discards. Measured across a sample of 106 profile 7 remuxes: **64 FEL to 42 MEL** — the
losing case is the common one.

Nothing in the container distinguishes them; MediaInfo reports both as
`BL+EL+RPU`. The RPU says, so `hibrit plan` declines to guess and the run
reports which it was as soon as it has read one.

## Tests

```
pytest -q -m "not tools and not real"   # logic only; no binaries, no media
pytest -q -m tools                      # real binaries, synthesised material
pytest -q -m real                       # real clips; set HIBRIT_MEDIA
pytest -q -m gui                        # drives the window; needs a display
pytest -q                               # everything but the window
```

Two environment variables decide how much of the `real` tier can run:

| Variable | Points at | Without it |
|---|---|---|
| `HIBRIT_MEDIA` | the directory holding the sample clips | the whole tier skips |
| `HIBRIT_FEL` | any profile 7 **FEL** remux | three tests skip |

`HIBRIT_FEL` is separate because no synthesised sample can stand in for it:
`dovi_tool generate` writes profiles 5, 8.1 and 8.4, and a FEL enhancement layer
is not among them. Those three tests spent their whole life skipped, and one of
them was asserting wording the planner had stopped producing — which nothing
noticed, because a skipped test reports success just as quietly as a passing
one. Point the variable at a real FEL film before trusting that tier.

The `tools` tier needs no media: `dovi_tool generate` writes an RPU from a JSON
config with no video involved, and ffmpeg builds a ten-second HDR10 clip in a
fifth of a second. That is what CI runs — so the code that shells out to the
external tools is covered there too, not only on one machine.

The `real` tier is the one that can fail in the most interesting way. It strips
the metadata off a real clip and puts it back, and asserts the result hashes
identically to the original.

Both of those tiers spend half their tests on inputs that should be **refused**:
a deliberately shortened RPU, a lengthened one, two unrelated films, a search
range too narrow to contain the answer. A test suite that only ever feeds a tool
matching pairs never sees what it does with the mistake its user is most likely
to make.

### One rule the suite is arranged around

**Anything that parses a tool's output is tested against that tool**, never
against a sample written from memory. This is not fastidiousness. The guard that
catches hdr10plus_tool's mismatch warning was written by analogy with
dovi_tool's, matched nothing, and was dead code for weeks — its tests fed it a
string I had invented, so they proved only that my invention parses. A flag that
does not exist (`--skip-validation`) survived the same way, one caller from
breaking a job.

So the transcripts in `tests/test_metadata_tools.py` are copied from real runs,
and a test in the `tools` tier re-parses what the tools say *now* and fails when
that stops matching what the transcripts assume.

## Licence

GPL-3.0. `dovi_tool` and `hdr10plus_tool` are separate programs by
[quietvoid](https://github.com/quietvoid), invoked as subprocesses; hibrit does
not vendor or modify them.
