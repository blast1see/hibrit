# hibrit

Move Dolby Vision and HDR10+ metadata from one release of a film to another,
without re-encoding a single frame.

*hibrit* is Turkish for "hybrid", after the release naming convention this
produces: `Title.2021.Hybrid.2160p.UHD.BluRay.REMUX.DV.HDR10Plus...`

> **Status: usable, and honest about what it cannot check.** The metadata
> transfer is proven byte-for-byte by the test suite. Whether the two releases
> you point it at *should* share metadata is a judgement no program can make;
> see [The failure nothing here can catch](#the-failure-nothing-here-can-catch).

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

`dovi_tool inject-rpu` does not stop when the metadata does not fit the video:

```
Warning: mismatched lengths. video 173802, RPU 5735
Metadata will be duplicated at the end to match video length
```

It prints that, **exits 0**, and writes a finished file. The file plays. Players
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

Both files are decoded to 128×72 grayscale — about 250 fps for 4K HEVC — and
reduced to one number per frame. Those two signals are cross-correlated and the
lag is the offset.

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
| `--verify-pixels`: strip both layers off the result *and* off the original, then compare | **not one bit of picture data changed** |

The last one is the real proof and costs two full rewrites, so it is opt-in.
Stripping both sides is not redundancy: `mkvextract` and `dovi_tool` write the
same NAL units as Annex B slightly differently (measured on one clip: 117,524,013
bytes versus 117,526,056), so comparing a raw extraction against a tool's output
would fail on a file where nothing changed. A check that fails when it should
pass is the worst kind, because the natural response is to stop believing it.

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

A 70 GB target needs its video stream extracted, injected into, and remuxed:
roughly three times the target's size in working space before anything is
deleted. `--workdir` is required rather than defaulted for exactly this reason —
the drive holding your sources is usually the one without the room. hibrit
checks free space before running the first command and deletes each intermediate
as soon as the next step has consumed it.

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

## Scope

**Does:** move existing Dolby Vision RPUs and HDR10+ metadata between releases,
convert profile 5 and profile 7 to single-layer 8.1, retime metadata to a
measured offset, remux, and verify.

**Does not:** generate Dolby Vision that does not exist (that is `cm_analyze`
territory and a different problem), re-encode anything, or preserve profile 7's
dual-layer structure as dual-layer.

## Tests

```
pytest -q                 # logic only; no binaries, no media
pytest -q -m tools        # needs dovi_tool, hdr10plus_tool, ffmpeg
pytest -q -m real         # needs real clips; set HIBRIT_MEDIA
```

The `real` tier is the one that can fail in an interesting way. It strips the
metadata off a real clip and puts it back, and asserts the result hashes
identically to the original. It also asserts that a deliberately shortened RPU
is **refused**, and that two unrelated films are **refused** — because a test
suite that only ever feeds a tool matching pairs never sees what it does with
the mistake its user is most likely to make.

## Licence

GPL-3.0. `dovi_tool` and `hdr10plus_tool` are separate programs by
[quietvoid](https://github.com/quietvoid), invoked as subprocesses; hibrit does
not vendor or modify them.
