"""Disk arithmetic and report semantics — the parts that must not be optimistic."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_info

from hibrit import pipeline
from hibrit.planner import build_plan
from hibrit.rpu import FrameCountMismatch
from hibrit.verify import Check, Report, picture_digest, sha256_file


class TestSpaceCheck:
    def test_refuses_before_the_first_command(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "big.mkv"
        target.write_bytes(b"x" * 1000)
        monkeypatch.setattr(pipeline, "free_space", lambda _: 100)
        with pytest.raises(pipeline.NotEnoughSpace) as caught:
            pipeline.check_space(tmp_path, target)
        assert caught.value.needed == 3000
        assert "--workdir" in str(caught.value)

    def test_allows_a_job_that_fits(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "big.mkv"
        target.write_bytes(b"x" * 1000)
        monkeypatch.setattr(pipeline, "free_space", lambda _: 10_000)
        pipeline.check_space(tmp_path, target)

    def test_free_space_walks_up_to_an_existing_parent(self, tmp_path) -> None:
        """The working directory may not exist yet when the check runs."""
        assert pipeline.free_space(tmp_path / "not" / "created" / "yet") > 0


class TestThrottleProgress:
    """Thinning the counters without losing the messages between them."""

    def test_a_line_per_percent_becomes_a_line_per_ten(self) -> None:
        seen: list[str] = []
        forward = pipeline.throttle_progress(seen.append)
        for percent in range(101):
            forward(f"Progress: {percent}%")
        assert len(seen) == 11
        assert seen[0].endswith("0%")
        assert seen[-1].endswith("100%")

    def test_translated_output_is_thinned_too(self) -> None:
        """mkvmerge speaks the system language.

        On the machine this was written for it reports "İlerleme: 42%", so a
        filter keyed to the word "Progress" would have passed every test written
        in English and thinned nothing in practice.
        """
        seen: list[str] = []
        forward = pipeline.throttle_progress(seen.append)
        for percent in range(101):
            forward(f"İlerleme: {percent}%")
        assert len(seen) == 11

    def test_everything_that_is_not_a_counter_passes_through(self) -> None:
        seen: list[str] = []
        forward = pipeline.throttle_progress(seen.append)
        forward("Parsing RPU file...")
        forward("Progress: 0%")
        forward("Rewriting file with interleaved RPU NALs..")
        forward("Progress: 3%")  # inside the same step, dropped
        forward("Warning: mismatched lengths. video 1000, RPU 800")
        assert seen == [
            "Parsing RPU file...",
            "Progress: 0%",
            "Rewriting file with interleaved RPU NALs..",
            "Warning: mismatched lengths. video 1000, RPU 800",
        ]

    def test_a_second_operation_starts_counting_again(self) -> None:
        """Four tools run in sequence and each restarts at zero. Without a reset
        the second one's counter would be silent all the way to 100."""
        seen: list[str] = []
        forward = pipeline.throttle_progress(seen.append)
        for percent in (0, 50, 100):
            forward(f"Progress: {percent}%")
        seen.clear()
        for percent in (0, 20, 60):
            forward(f"Progress: {percent}%")
        assert len(seen) == 3


class TestRunRefusals:
    def test_a_blocked_plan_never_starts(self, tmp_path) -> None:
        plan = build_plan(make_info("a.mkv"), make_info("b.mkv"))
        with pytest.raises(pipeline.PipelineError, match="cannot run"):
            pipeline.run(plan, tmp_path / "out.mkv", workdir=tmp_path)


class TestLeftovers:
    """What a stopped job says about the disk it was using.

    Nothing is deleted — a half-written stream is evidence, and tidying up
    quietly is not this program's posture — but at 70 GB a run that dies after
    two passes has left 140 GB behind, and a user who is not told meets it as a
    full disk some other day.
    """

    def test_it_names_the_files_and_the_total(self, tmp_path) -> None:
        (tmp_path / "target.hevc").write_bytes(b"x" * 3000)
        (tmp_path / "with_dv.hevc").write_bytes(b"y" * 5000)
        summary = pipeline.describe_leftovers(tmp_path)
        assert "2 file(s)" in summary
        assert "target.hevc" in summary and "with_dv.hevc" in summary
        assert str(tmp_path) in summary
        assert "Nothing was deleted" in summary

    def test_an_empty_directory_says_nothing(self, tmp_path) -> None:
        """No message beats a message about nothing."""
        assert pipeline.describe_leftovers(tmp_path) == ""

    def test_a_failed_run_reports_what_it_left(self, tmp_path, monkeypatch) -> None:
        """And the original exception still arrives with its own type."""
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("b.mkv", frames=1000)
        plan = build_plan(source, target)
        assert plan.ok

        workdir = tmp_path / "work"

        def fake_extract(_target, out, _box, progress=None):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"a partly written stream")
            raise FrameCountMismatch(1000, 800)

        monkeypatch.setattr(pipeline, "extract_video", fake_extract)
        monkeypatch.setattr(pipeline.Toolbox, "missing_required", lambda _self: [])

        said: list[str] = []
        with pytest.raises(FrameCountMismatch):
            pipeline.run(
                plan,
                tmp_path / "out.mkv",
                workdir=workdir,
                progress=said.append,
                skip_space_check=True,
            )

        stopped = [line for line in said if line.startswith("stopped.")]
        assert stopped, said
        assert "target.hevc" in stopped[0]

    def test_an_interrupted_run_reports_too(self, tmp_path, monkeypatch) -> None:
        """Ctrl-C at 60 GB in leaves exactly as much behind as a crash."""
        plan = build_plan(
            make_info("a.mkv", dv=True, dv_profile=8, frames=1000),
            make_info("b.mkv", frames=1000),
        )

        def interrupted(_target, out, _box, progress=None):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"half a stream")
            raise KeyboardInterrupt

        monkeypatch.setattr(pipeline, "extract_video", interrupted)
        monkeypatch.setattr(pipeline.Toolbox, "missing_required", lambda _self: [])

        said: list[str] = []
        with pytest.raises(KeyboardInterrupt):
            pipeline.run(
                plan,
                tmp_path / "out.mkv",
                workdir=tmp_path / "work",
                progress=said.append,
                skip_space_check=True,
            )
        assert any(line.startswith("stopped.") for line in said)


class TestReport:
    def test_a_failure_sinks_the_report(self) -> None:
        report = Report(
            checks=(
                Check("a", True, "fine"),
                Check("b", False, "not fine"),
            )
        )
        assert not report.passed
        assert len(report.failures) == 1
        assert "Do not keep this output" in report.describe()

    def test_a_skipped_check_is_reported_as_unmeasured_not_passed(self) -> None:
        report = Report(checks=(Check("a", True, "fine"), Check("b", True, "-", skipped=True)))
        assert report.passed
        assert [c.name for c in report.unmeasured] == ["b"]
        assert "Not measured: b" in report.describe()

    def test_everything_measured_and_passing_says_so_plainly(self) -> None:
        assert Report(checks=(Check("a", True, "fine"),)).describe().endswith("All checks passed.")


def annex_b(units: list[tuple[int, bytes]], *, long_start: bool = True) -> bytes:
    """Assemble an Annex B stream from ``(nal_type, payload)`` pairs."""
    start = b"\x00\x00\x00\x01" if long_start else b"\x00\x00\x01"
    out = b""
    for nal_type, payload in units:
        header = bytes([(nal_type << 1) & 0xFF, 0x01])
        out += start + header + payload
    return out


class TestPictureDigest:
    """The parser that decides whether the picture changed.

    It is worth testing directly because its failure mode is silence: a bug
    that skipped every unit would hash nothing consistently and report every
    file as identical to every other.
    """

    def test_counts_only_the_picture_carrying_units(self, tmp_path) -> None:
        path = tmp_path / "stream.hevc"
        path.write_bytes(
            annex_b(
                [
                    (32, b"\xaa" * 8),  # VPS
                    (33, b"\xbb" * 8),  # SPS
                    (1, b"\x11" * 40),  # slice
                    (39, b"\xcc" * 8),  # SEI
                    (1, b"\x22" * 40),  # slice
                ]
            )
        )
        _, count = picture_digest(path)
        assert count == 2

    def test_metadata_added_around_the_picture_does_not_change_the_digest(self, tmp_path) -> None:
        """The whole point. dovi_tool adds a 7-byte access unit delimiter per
        frame and RPU units alongside; none of that is picture data."""
        slices = [(1, b"\x11" * 40), (1, b"\x22" * 40)]
        bare = tmp_path / "bare.hevc"
        dressed = tmp_path / "dressed.hevc"
        bare.write_bytes(annex_b([(33, b"\xbb" * 8), *slices]))
        dressed.write_bytes(
            annex_b(
                [
                    (35, b"\x50"),  # access unit delimiter
                    (33, b"\xbb" * 8),
                    slices[0],
                    (62, b"\xde\xad" * 20),  # Dolby Vision RPU
                    (35, b"\x50"),
                    slices[1],
                    (39, b"\xcc" * 30),  # HDR10+ SEI
                ]
            )
        )
        assert picture_digest(bare) == picture_digest(dressed)

    def test_a_changed_slice_does_change_the_digest(self, tmp_path) -> None:
        a = tmp_path / "a.hevc"
        b = tmp_path / "b.hevc"
        a.write_bytes(annex_b([(1, b"\x11" * 40)]))
        b.write_bytes(annex_b([(1, b"\x11" * 39 + b"\x12")]))
        assert picture_digest(a) != picture_digest(b)

    def test_three_and_four_byte_start_codes_are_equivalent(self, tmp_path) -> None:
        units = [(33, b"\xbb" * 8), (1, b"\x11" * 40), (1, b"\x22" * 40)]
        long_form = tmp_path / "long.hevc"
        short_form = tmp_path / "short.hevc"
        long_form.write_bytes(annex_b(units, long_start=True))
        short_form.write_bytes(annex_b(units, long_start=False))
        assert picture_digest(long_form) == picture_digest(short_form)

    def test_a_start_code_split_across_reads_is_still_found(self, tmp_path) -> None:
        """Read boundaries are arbitrary; a 68 GB file has thousands of them."""
        path = tmp_path / "stream.hevc"
        path.write_bytes(annex_b([(1, b"\x11" * 500), (1, b"\x22" * 500)]))
        whole = picture_digest(path)
        for chunk in (1, 2, 3, 4, 5, 7, 64, 503):
            assert picture_digest(path, chunk=chunk) == whole, f"chunk={chunk}"

    def test_a_container_is_refused_rather_than_parsed(self, tmp_path) -> None:
        """The failure this guard exists for is a confident wrong answer.

        Matroska stores HEVC length-prefixed, so scanning one for start codes
        finds byte patterns that happen to occur inside compressed data. Doing
        it to a 72 GB remux turned up 303,436 of them and produced a hash of
        nothing in particular — which would have compared equal to itself and
        unequal to everything else, exactly like a working check.
        """
        for suffix in (".mkv", ".mp4", ".webm"):
            path = tmp_path / f"movie{suffix}"
            path.write_bytes(annex_b([(1, b"\x11" * 40)]))
            with pytest.raises(ValueError, match="container"):
                picture_digest(path)

    def test_a_bare_stream_is_parsed(self, tmp_path) -> None:
        for suffix in (".hevc", ".h265", ".bin", ""):
            path = tmp_path / f"stream{suffix}"
            path.write_bytes(annex_b([(1, b"\x11" * 40)]))
            assert picture_digest(path)[1] == 1

    def test_an_empty_file_is_not_a_match_for_everything(self, tmp_path) -> None:
        empty = tmp_path / "empty.hevc"
        empty.write_bytes(b"")
        real = tmp_path / "real.hevc"
        real.write_bytes(annex_b([(1, b"\x11" * 40)]))
        assert picture_digest(empty)[1] == 0
        assert picture_digest(empty) != picture_digest(real)


def test_sha256_matches_hashlib(tmp_path) -> None:
    import hashlib

    path = tmp_path / "blob.bin"
    payload = bytes(range(256)) * 5000
    path.write_bytes(payload)
    assert sha256_file(path, chunk=1024) == hashlib.sha256(payload).hexdigest()


class TestContainerSignalling:
    """The one thing a player reads before it reads the stream.

    mkvmerge derives a Dolby Vision configuration record from the injected
    bitstream and writes it into the Matroska track header. A player consults
    that record to decide whether to engage Dolby Vision at all, so a file can
    carry a perfect RPU and still play as plain HDR10 if the record is missing —
    and every other check in verify.py inspects the bitstream, where it would
    see nothing wrong.
    """

    from conftest import make_info as _make_info

    @staticmethod
    def _with_track(**fields):
        from conftest import make_info

        info = make_info("out.mkv", dv=True, dv_profile=8)
        object.__setattr__(info, "track", fields)
        return info

    def test_a_complete_record_passes_and_says_what_it_advertises(self) -> None:
        from hibrit.verify import _check_container_signalling

        checks = _check_container_signalling(
            self._with_track(
                HDR_Format_Profile="dvhe.08 / ",
                HDR_Format_Level="06 / ",
                HDR_Format_Settings="BL+RPU / ",
            )
        )
        assert len(checks) == 1
        assert checks[0].passed
        assert "dvhe.08" in checks[0].detail
        assert "BL+RPU" in checks[0].detail

    def test_a_missing_record_fails_and_says_what_that_means(self) -> None:
        """Not "field absent" — what the user would see if they played it."""
        from hibrit.verify import _check_container_signalling

        checks = _check_container_signalling(self._with_track())
        assert len(checks) == 1
        assert not checks[0].passed
        assert "plain HDR10" in checks[0].detail

    def test_a_partial_record_names_the_missing_fields(self) -> None:
        from hibrit.verify import _check_container_signalling

        checks = _check_container_signalling(self._with_track(HDR_Format_Profile="dvhe.08 / "))
        assert not checks[0].passed
        assert "HDR_Format_Level" in checks[0].detail
        assert "HDR_Format_Settings" in checks[0].detail

    def test_a_file_without_dolby_vision_is_not_asked_about_it(self) -> None:
        """HDR10+-only transfers have no configuration record to carry, and a
        check that cannot apply should not appear at all."""
        from conftest import make_info

        from hibrit.verify import _check_container_signalling

        assert _check_container_signalling(make_info("out.mkv", hdr10plus=True)) == []


class TestStaleLabels:
    """Release groups write the metadata into the video track's name.

    A real remux carries "MPEG-H HEVC Video / 59681 kbps /
    2160p / ... / HDR10+ Profile B / Dolby Vision MEL @ 69 kbps". Convert that
    to single-layer 8.1 and the label is wrong. Editing someone's label is not
    this program's business and leaving a false one is worse than dropping it,
    so it is kept and reported.
    """

    from hibrit.matroska import stale_label_warning as _warn

    @staticmethod
    def _result(**kwargs):
        from conftest import make_info

        return make_info("out.mkv", **kwargs)

    def test_the_real_name_after_a_conversion_to_81(self) -> None:
        from hibrit.matroska import stale_label_warning

        name = (
            "MPEG-H HEVC Video / 59681 kbps / 2160p / 23.976 fps / 16:9 / "
            "Main 10 @ Level 5.1 @ High / 10 bits / 4000nits / HDR10+ Profile B / "
            "Dolby Vision MEL @ 69 kbps"
        )
        warning = stale_label_warning(name, self._result(dv=True, dv_profile=8))
        assert warning is not None
        assert "enhancement layer" in warning
        assert "HDR10+" in warning
        assert "The file is right; the label is not." in warning

    def test_hdr10plus_added_to_a_name_that_does_not_mention_it(self) -> None:
        from hibrit.matroska import stale_label_warning

        warning = stale_label_warning(
            "UHD BluRay Remux / HDR10", self._result(dv=True, dv_profile=8, hdr10plus=True)
        )
        assert warning is not None
        assert "does not mention" in warning

    def test_a_profile_number_that_no_longer_matches(self) -> None:
        from hibrit.matroska import stale_label_warning

        warning = stale_label_warning("Dolby Vision Profile 7", self._result(dv=True, dv_profile=8))
        assert warning is not None
        assert "profile 7" in warning

    def test_a_name_that_still_describes_the_file_says_nothing(self) -> None:
        from hibrit.matroska import stale_label_warning

        assert (
            stale_label_warning(
                "UHD BluRay Remux / HDR10+ / Dolby Vision",
                self._result(dv=True, dv_profile=8, hdr10plus=True),
            )
            is None
        )

    def test_no_name_is_not_a_stale_name(self) -> None:
        from hibrit.matroska import stale_label_warning

        assert stale_label_warning(None, self._result(dv=True, dv_profile=8)) is None
        assert stale_label_warning("", self._result(dv=True, dv_profile=8)) is None

    def test_a_plain_descriptive_name_is_left_alone(self) -> None:
        """Most names say nothing about metadata and must not be nagged about."""
        from hibrit.matroska import stale_label_warning

        assert stale_label_warning("Blu-ray Remux", self._result(dv=True, dv_profile=8)) is None


class TestContainerFailures:
    """The paths that fire when a tool goes wrong on a 70 GB file.

    They are defensive, which is exactly why they need exercising: a defence
    that has never run is a guess about what happens.
    """

    @staticmethod
    def _box(returncode=0, stdout="", writes=None):
        """A Toolbox whose run() does what the test needs and nothing else."""
        import subprocess

        class FakeBox:
            def run(self, name, args, **kwargs):
                if writes is not None:
                    writes()
                return subprocess.CompletedProcess(args, returncode, stdout, stdout)

        return FakeBox()

    def test_an_extraction_that_writes_nothing_is_an_error(self, tmp_path) -> None:
        from conftest import make_info

        from hibrit.matroska import ContainerError, extract_video

        info = make_info(str(tmp_path / "in.mkv"))
        with pytest.raises(ContainerError, match="no video stream"):
            extract_video(info, tmp_path / "out.hevc", self._box())

    def test_an_extraction_that_writes_an_empty_file_is_too(self, tmp_path) -> None:
        """Worse than nothing: a zero-byte file looks like a result."""
        from conftest import make_info

        from hibrit.matroska import ContainerError, extract_video

        out = tmp_path / "out.hevc"
        info = make_info(str(tmp_path / "in.mkv"))
        with pytest.raises(ContainerError):
            extract_video(info, out, self._box(writes=lambda: out.write_bytes(b"")))

    def test_a_raw_stream_is_handed_back_rather_than_copied(self, tmp_path) -> None:
        """These files are the size of the films they came from."""
        from conftest import make_info

        from hibrit.matroska import extract_video

        source = tmp_path / "already.hevc"
        source.write_bytes(b"annex b")
        info = make_info(str(source))
        assert extract_video(info, tmp_path / "unused.hevc", self._box()) == source
        assert not (tmp_path / "unused.hevc").exists()

    def test_mkvmerge_exiting_two_is_a_failure_and_the_output_goes(self, tmp_path) -> None:
        """Exit 1 is a warning worth ignoring; 2 is not, and a partial file left
        behind would look like a result."""
        from conftest import make_info

        from hibrit.matroska import ContainerError, remux

        out = tmp_path / "out.mkv"
        box = self._box(
            returncode=2, stdout="Error: something", writes=lambda: out.write_bytes(b"x")
        )
        with pytest.raises(ContainerError, match="mkvmerge failed"):
            remux(tmp_path / "v.hevc", make_info(str(tmp_path / "d.mkv")), out, box)
        assert not out.exists()

    def test_mkvmerge_exiting_one_is_tolerated(self, tmp_path) -> None:
        from conftest import make_info

        from hibrit.matroska import remux

        out = tmp_path / "out.mkv"
        box = self._box(
            returncode=1, stdout="Warning: reordered", writes=lambda: out.write_bytes(b"x")
        )
        assert remux(tmp_path / "v.hevc", make_info(str(tmp_path / "d.mkv")), out, box) == out

    def test_the_track_id_falls_back_when_the_container_does_not_say(self) -> None:
        """MediaInfo omits StreamOrder on some files; zero is the overwhelming
        majority and a wrong guess surfaces immediately as a failed extraction,
        not as a quiet mistake."""
        from conftest import make_info

        from hibrit.matroska import video_track_id

        info = make_info("x.mkv")
        object.__setattr__(info, "track", {})
        assert video_track_id(info) == 0

        object.__setattr__(info, "track", {"StreamOrder": "2"})
        assert video_track_id(info) == 2

        object.__setattr__(info, "track", {"StreamOrder": "not a number"})
        assert video_track_id(info) == 0
