"""Disk arithmetic and report semantics — the parts that must not be optimistic."""

from __future__ import annotations

import pytest
from conftest import make_info

from hibrit import pipeline
from hibrit.planner import build_plan
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
