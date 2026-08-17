"""Driving the window without a person in front of it.

The window has already produced two defects that the command line could not:
it ignored a path typed into its entry boxes, and it printed "needs about 0 GB"
for a clip. Both were in the wiring between widgets and core, which is exactly
what a test can reach and a reader cannot.

These are marked ``gui`` as well as ``real``: Tk needs a display, and CI has
none. Run them with ``pytest -m gui``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.gui, pytest.mark.real]

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="session")
def hdr10_target(media: Path, toolbox, tmp_path_factory) -> Path:
    """A target with no Dolby Vision, built from a real clip.

    Built rather than found: a fixture that leans on whatever happens to be
    left in the media directory breaks the day somebody tidies it up.
    """
    from hibrit.rpu import DoviTool

    clip = media / "p8_clip.hevc"
    if not clip.exists():
        pytest.skip(f"p8_clip.hevc not in {media}")

    workdir = tmp_path_factory.mktemp("gui-target")
    clean = DoviTool(toolbox).remove(clip, workdir / "clean.hevc")
    target = workdir / "target.mkv"
    toolbox.run("mkvmerge", ["-q", "-o", str(target), str(clean)], check=False)
    clean.unlink(missing_ok=True)
    return target


@pytest.fixture
def app(toolbox):
    """A live App on a withdrawn root, torn down whatever the test does."""
    from hibrit.gui import App

    try:
        root = tk.Tk()
    except tk.TclError as error:  # pragma: no cover - depends on the machine
        pytest.skip(f"no display: {error}")
    root.withdraw()
    instance = App(root)
    root.update()
    try:
        yield instance
    finally:
        root.destroy()


def _pump(app, root_update, *, until, limit: int = 6000) -> None:
    """Run the message pump until *until* returns true, or give up.

    The window's worker posts to a queue that ``_drain`` empties on a timer.
    Nothing here waits on the thread directly: a test that joins the thread
    would pass even if the queue wiring were broken, which is the part most
    likely to be wrong.
    """
    import time

    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        app._drain_once()
        root_update()
        if until():
            return
        time.sleep(0.05)
    raise AssertionError("the window never finished")


class TestDiagnosis:
    def test_a_pair_that_needs_no_alignment_is_ready_to_run(
        self, app, media: Path, hdr10_target: Path, tmp_path: Path
    ) -> None:
        app.source_path.set(str(media / "align_a.mkv"))
        app.target_path.set(str(hdr10_target))
        app.workdir_path.set(str(tmp_path))
        app.output_path.set(str(tmp_path / "out.mkv"))
        app._reprobe()

        assert app.plan is not None and app.plan.ok
        assert "not needed" in app.alignment_text.get()
        assert str(app.run_button["state"]) == "normal"

    def test_a_pair_that_needs_alignment_will_not_run_unmeasured(
        self, app, media: Path, tmp_path: Path
    ) -> None:
        """The window must not let an unmeasured offset through, because the
        result would look finished and be wrong for the whole runtime."""
        app.source_path.set(str(media / "align_a.mkv"))
        app.target_path.set(str(media / "align_b.mkv"))
        app.workdir_path.set(str(tmp_path))
        app.output_path.set(str(tmp_path / "out.mkv"))
        app._reprobe()

        assert app.plan is not None
        assert str(app.run_button["state"]) == "disabled"

    def test_free_space_is_readable_at_clip_scale(
        self, app, media: Path, hdr10_target: Path, tmp_path: Path
    ) -> None:
        """A 112 MB clip once reported "needs about 0 GB"."""
        app.source_path.set(str(media / "align_a.mkv"))
        app.target_path.set(str(hdr10_target))
        app.workdir_path.set(str(tmp_path))
        app._reprobe()

        label = app.space_label.get()
        assert "needs about" in label
        assert "about 0 GB" not in label


class TestRunThroughTheWindow:
    def test_the_whole_job_runs_and_reports_a_clean_verification(
        self, app, media: Path, hdr10_target: Path, toolbox, tmp_path: Path
    ) -> None:
        """Plan, run and verify, driven the way a person drives it.

        The target was built by stripping a real clip — a route the window never
        takes — so the expected outcome is known before the run.
        """
        target = hdr10_target
        app.source_path.set(str(media / "align_a.mkv"))
        app.target_path.set(str(target))
        app.workdir_path.set(str(tmp_path / "work"))
        app.output_path.set(str(tmp_path / "out.mkv"))
        app._reprobe()
        assert app.plan is not None and app.plan.ok

        root = app.winfo_toplevel()
        app._run()
        _pump(app, root.update, until=lambda: app.finished)

        log = app.log_text.get("1.0", "end")
        assert "Verification failed" not in log, log
        assert "All measured checks passed" in log, log

        # The log is what the user reads, but it is not evidence on its own:
        # check the file the window claims to have written.
        from hibrit.probe import probe

        out = tmp_path / "out.mkv"
        assert out.exists()
        produced = probe(out, toolbox)
        assert produced.has_dv
        assert produced.dv_profile == 8
        assert produced.frame_count == probe(target, toolbox).frame_count

    def test_a_failure_reaches_the_log_instead_of_the_void(
        self, app, media: Path, hdr10_target: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """A worker thread that dies silently leaves the window looking busy
        forever. Whatever went wrong has to arrive somewhere the user reads."""
        from hibrit import gui

        monkeypatch.setattr(gui, "messagebox", _SilentMessagebox())

        app.source_path.set(str(media / "align_a.mkv"))
        app.target_path.set(str(hdr10_target))
        app.workdir_path.set(str(tmp_path / "work"))
        app.output_path.set(str(tmp_path / "out.mkv"))
        app._reprobe()

        def explode(*_args, **_kwargs):
            raise RuntimeError("dovi_tool fell over")

        monkeypatch.setattr(gui, "run", explode)

        root = app.winfo_toplevel()
        app._run()
        _pump(app, root.update, until=lambda: app.finished)

        log = app.log_text.get("1.0", "end")
        assert "dovi_tool fell over" in log
        assert str(app.run_button["state"]) == "normal", "the window stayed stuck"


class _SilentMessagebox:
    """Modal dialogs block the test; the log is what is being checked anyway."""

    @staticmethod
    def showerror(*_args, **_kwargs) -> None:
        return None

    @staticmethod
    def showinfo(*_args, **_kwargs) -> None:
        return None


class TestOverrideCannotForceNothing:
    """The override box forces a figure through. A refusal without a figure has
    none, so there is nothing for it to force — and the pipeline would reject it
    anyway, now before any work rather than after extracting the stream."""

    def _apply(self, app, alignment):
        app._apply_alignment(alignment)
        return str(app.override_check["state"])

    def test_a_refusal_with_a_candidate_offers_the_override(self, app) -> None:
        from hibrit.align import Alignment, Verdict

        state = self._apply(
            app,
            Alignment(
                offset=-115, verdict=Verdict.NO_MATCH, confidence=1.04, windows=(), reason="noise"
            ),
        )
        assert state == "normal"

    def test_a_refusal_with_no_offset_does_not(self, app) -> None:
        from hibrit.align import Alignment, Verdict

        state = self._apply(
            app,
            Alignment(
                offset=None, verdict=Verdict.NO_MATCH, confidence=1.1, windows=(), reason="disagree"
            ),
        )
        assert state == "disabled"

    def test_a_trusted_result_does_not_either(self, app) -> None:
        """Nothing to override when the measurement is already believed."""
        from hibrit.align import Alignment, Verdict

        state = self._apply(
            app,
            Alignment(
                offset=137, verdict=Verdict.RELIABLE, confidence=3.7, windows=(), reason="ok"
            ),
        )
        assert state == "disabled"

    def test_run_stays_disabled_while_the_offset_is_missing(
        self, app, media, hdr10_target, tmp_path
    ) -> None:
        from hibrit.align import Alignment, Verdict

        app.source_path.set(str(media / "align_a.mkv"))
        app.target_path.set(str(media / "align_b.mkv"))
        app.workdir_path.set(str(tmp_path))
        app.output_path.set(str(tmp_path / "out.mkv"))
        app._reprobe()

        app._apply_alignment(
            Alignment(
                offset=None, verdict=Verdict.NO_MATCH, confidence=1.1, windows=(), reason="disagree"
            )
        )
        assert str(app.run_button["state"]) == "disabled"


class TestVerificationInTheWindow:
    """What the window shows when the checks do not all pass.

    A failed verification is the one outcome where the user must not keep the
    file, and the window is where they would read that. Covered here because
    the existing tests reach a worker that throws, which is a different path:
    a report that fails arrives through the queue as a result, not an error.
    """

    @staticmethod
    def _report(*checks):
        from hibrit.verify import Report

        return Report(checks=tuple(checks))

    def test_a_failure_is_named_and_the_verdict_is_unambiguous(self, app) -> None:
        from hibrit.verify import Check

        app._show_report(
            self._report(
                Check("dolby vision round-trip", True, "byte-for-byte"),
                Check("picture untouched", False, "the coded picture data differs"),
            )
        )
        log = app.log_text.get("1.0", "end")
        assert "[PASS] dolby vision round-trip" in log
        assert "[FAIL] picture untouched" in log
        assert "Do not keep this output" in log
        assert "All measured checks passed" not in log

    def test_a_clean_report_says_so(self, app) -> None:
        from hibrit.verify import Check

        app._show_report(self._report(Check("frame count preserved", True, "1000 == 1000")))
        log = app.log_text.get("1.0", "end")
        assert "All measured checks passed" in log
        assert "Do not keep" not in log

    def test_an_unmeasured_check_is_neither_a_pass_nor_a_failure(self, app) -> None:
        """A skipped check must not read as green. It is reported as SKIP and
        does not stop the report from passing."""
        from hibrit.verify import Check

        app._show_report(
            self._report(
                Check("frame count preserved", True, "1000 == 1000"),
                Check("picture untouched", True, "not run", skipped=True),
            )
        )
        log = app.log_text.get("1.0", "end")
        assert "[SKIP] picture untouched" in log
        assert "All measured checks passed" in log

    def test_the_failure_colour_is_the_one_used_for_blockers(self, app) -> None:
        """The window has three tags and they have to mean what they look like."""
        from hibrit.gui import NOTE_COLOURS
        from hibrit.planner import Level

        assert app.log_text.tag_cget("fail", "foreground") == NOTE_COLOURS[Level.BLOCKER]
