"""Tkinter window for the whole flow: diagnose, plan, align, run, verify.

One deliberate choice about presentation. An earlier tool in this house showed
its measured offset as a single large number at the top of the window, and that
number got believed regardless of how weak the measurement behind it was. Here
the offset, its confidence and the number of places it was measured are printed
in the same font at the same size, in one line, because they are one fact. A
result that is not trustworthy cannot be approved without ticking a box that
says so in words.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from hibrit import __version__
from hibrit.align import Alignment, align
from hibrit.pipeline import SPACE_FACTOR, NotEnoughSpace, free_space, run
from hibrit.planner import Level, Plan, build_plan
from hibrit.probe import VideoInfo, probe
from hibrit.tools import Toolbox
from hibrit.verify import verify

VIDEO_TYPES = [("Matroska", "*.mkv"), ("HEVC bitstream", "*.hevc *.h265"), ("All files", "*.*")]

NOTE_COLOURS = {
    Level.BLOCKER: "#b00020",
    Level.WARNING: "#a86300",
    Level.INFO: "#4a4a4a",
}


def _gigabytes(size: int | float) -> str:
    """Sizes here span a test clip and a 70 GB remux, so the precision moves.

    Rounding a 112 MB clip to whole gigabytes prints "0 GB", which reads as a
    bug rather than as a small file.
    """
    gb = size / 2**30
    return f"{gb:.2f} GB" if gb < 10 else f"{gb:.0f} GB"


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.box = Toolbox()
        self.source_path = tk.StringVar()
        self.target_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.workdir_path = tk.StringVar()
        self.space_label = tk.StringVar(value="")
        self.alignment_text = tk.StringVar(value="not measured")
        self.override = tk.BooleanVar(value=False)
        # On by default: the check reads the two streams once and compares only
        # their coded picture units, so it costs a read rather than a rewrite.
        self.verify_pixels = tk.BooleanVar(value=True)

        self.source_info: VideoInfo | None = None
        self.target_info: VideoInfo | None = None
        self.plan: Plan | None = None
        self.alignment: Alignment | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        #: Set when a run reaches its end, however it ended. A worker that dies
        #: silently would otherwise leave the window looking busy forever.
        self.finished = False

        self._build()
        self.after(100, self._drain)

    # -- layout ---------------------------------------------------------------

    def _build(self) -> None:
        row = 0
        row = self._file_row(
            row,
            "Source (has the metadata)",
            self.source_path,
            self._pick_source,
            on_commit=self._reprobe,
        )
        row = self._file_row(
            row,
            "Target (keeps its picture)",
            self.target_path,
            self._pick_target,
            on_commit=self._reprobe,
        )

        work = ttk.Frame(self)
        work.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        work.columnconfigure(1, weight=1)
        ttk.Label(work, text="Working directory").grid(row=0, column=0, sticky="w")
        work_entry = ttk.Entry(work, textvariable=self.workdir_path)
        work_entry.grid(row=0, column=1, sticky="ew", padx=6)
        work_entry.bind("<FocusOut>", lambda _event: self._show_space())
        ttk.Button(work, text="Browse", command=self._pick_workdir).grid(row=0, column=2)
        ttk.Label(work, textvariable=self.space_label, foreground="#4a4a4a").grid(
            row=1, column=1, sticky="w", padx=6
        )
        row += 1

        row = self._file_row(row, "Output file", self.output_path, self._pick_output)

        # The Run button depends on these two being filled in, so it has to
        # notice them being filled in. Without the traces, typing a valid path
        # leaves the button greyed out and says nothing about why.
        self.output_path.trace_add("write", lambda *_: self._refresh_run_button())
        self.workdir_path.trace_add("write", lambda *_: self._refresh_run_button())

        # --- plan ---------------------------------------------------------------
        plan_frame = ttk.LabelFrame(self, text="Plan", padding=8)
        plan_frame.grid(row=row, column=0, sticky="nsew", pady=(10, 0))
        plan_frame.columnconfigure(0, weight=1)
        plan_frame.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=1)
        self.plan_text = tk.Text(plan_frame, height=12, wrap="word", state="disabled")
        self.plan_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(plan_frame, command=self.plan_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.plan_text["yscrollcommand"] = scroll.set
        for level, colour in NOTE_COLOURS.items():
            self.plan_text.tag_configure(level.value, foreground=colour)
        self.plan_text.tag_configure("heading", font=("TkDefaultFont", 9, "bold"))
        row += 1

        # --- alignment ----------------------------------------------------------
        align_frame = ttk.LabelFrame(self, text="Frame alignment", padding=8)
        align_frame.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        align_frame.columnconfigure(1, weight=1)
        self.align_button = ttk.Button(
            align_frame, text="Measure", command=self._measure, state="disabled"
        )
        self.align_button.grid(row=0, column=0)
        # Offset, confidence and window count share one label and one font size.
        ttk.Label(align_frame, textvariable=self.alignment_text).grid(
            row=0, column=1, sticky="w", padx=8
        )
        self.override_check = ttk.Checkbutton(
            align_frame,
            text="Use this offset anyway, even though it is not trustworthy",
            variable=self.override,
            command=self._refresh_run_button,
            state="disabled",
        )
        self.override_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1

        # --- actions ------------------------------------------------------------
        actions = ttk.Frame(self)
        actions.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        self.run_button = ttk.Button(actions, text="Run", command=self._run, state="disabled")
        self.run_button.grid(row=0, column=0)
        ttk.Checkbutton(
            actions,
            text="prove the picture is unchanged (one extra read of each stream)",
            variable=self.verify_pixels,
        ).grid(row=0, column=1, sticky="w", padx=8)
        row += 1

        # --- log ----------------------------------------------------------------
        log_frame = ttk.LabelFrame(self, text="Log", padding=8)
        log_frame.grid(row=row, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=1)
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text["yscrollcommand"] = log_scroll.set
        self.log_text.tag_configure("fail", foreground=NOTE_COLOURS[Level.BLOCKER])
        self.log_text.tag_configure("pass", foreground="#1a6b2a")

    def _file_row(
        self, row: int, label: str, variable: tk.StringVar, command, *, on_commit=None
    ) -> int:
        frame = ttk.Frame(self)
        frame.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label, width=26).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=variable)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Browse", command=command).grid(row=0, column=2)

        # A path typed or pasted into the box has to work as well as one chosen
        # from the dialog. Probing on every keystroke would run mediainfo on
        # half-typed paths, so it happens when the field is committed instead.
        if on_commit is not None:
            entry.bind("<Return>", lambda _event: on_commit())
            entry.bind("<FocusOut>", lambda _event: on_commit())
        return row + 1

    # -- pickers --------------------------------------------------------------

    def _pick_source(self) -> None:
        path = filedialog.askopenfilename(title="Source", filetypes=VIDEO_TYPES)
        if path:
            self.source_path.set(path)
            self._reprobe()

    def _pick_target(self) -> None:
        path = filedialog.askopenfilename(title="Target", filetypes=VIDEO_TYPES)
        if path:
            self.target_path.set(path)
            if not self.output_path.get():
                candidate = Path(path)
                self.output_path.set(str(candidate.with_name(candidate.stem + ".hibrit.mkv")))
            self._reprobe()

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Output", defaultextension=".mkv", filetypes=VIDEO_TYPES
        )
        if path:
            self.output_path.set(path)

    def _pick_workdir(self) -> None:
        path = filedialog.askdirectory(title="Working directory")
        if path:
            self.workdir_path.set(path)
            self._show_space()

    def _show_space(self) -> None:
        directory = self.workdir_path.get()
        if not directory:
            self.space_label.set("")
            return
        available = free_space(Path(directory))
        need = ""
        if self.target_info is not None and self.target_info.path.exists():
            required = self.target_info.path.stat().st_size * SPACE_FACTOR
            verdict = "" if available >= required else "  — NOT ENOUGH"
            need = f"; this job needs about {_gigabytes(required)}{verdict}"
        self.space_label.set(f"{_gigabytes(available)} free{need}")

    # -- planning -------------------------------------------------------------

    def _reprobe(self) -> None:
        source, target = self.source_path.get(), self.target_path.get()
        if not source or not target:
            return
        try:
            self.source_info = probe(Path(source), self.box)
            self.target_info = probe(Path(target), self.box)
            self.plan = build_plan(self.source_info, self.target_info)
        except Exception as error:
            messagebox.showerror("Could not read the files", str(error))
            return

        self._render_plan(self.plan)
        self._show_space()
        self.alignment = None
        self.override.set(False)
        self.alignment_text.set(
            "not measured" if self.plan.needs_alignment else "not needed — frame counts match"
        )
        self.align_button["state"] = "normal" if self.plan.needs_alignment else "disabled"
        self.override_check["state"] = "disabled"
        self._refresh_run_button()

    def _render_plan(self, plan: Plan) -> None:
        self.plan_text["state"] = "normal"
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("end", f"{plan.source.name}\n", "heading")
        self.plan_text.insert("end", f"  {plan.source.describe()}\n")
        self.plan_text.insert("end", f"{plan.target.name}\n", "heading")
        self.plan_text.insert("end", f"  {plan.target.describe()}\n\n")

        moved = ", ".join(k.label for k in plan.transfer) or "nothing"
        self.plan_text.insert("end", f"Transfer: {moved}\n\n", "heading")
        for note in plan.notes:
            self.plan_text.insert("end", f"{note.describe()}\n\n", note.level.value)
        if plan.ok:
            self.plan_text.insert("end", "Steps\n", "heading")
            for index, step in enumerate(plan.steps, start=1):
                self.plan_text.insert("end", f"  {index}. {step.summary}\n")
                self.plan_text.insert("end", f"     {step.reason}\n")
        self.plan_text["state"] = "disabled"

    def _refresh_run_button(self) -> None:
        ready = (
            self.plan is not None
            and self.plan.ok
            and bool(self.output_path.get())
            and bool(self.workdir_path.get())
        )
        if ready and self.plan is not None and self.plan.needs_alignment:
            ready = self.alignment is not None and (self.alignment.usable or self.override.get())
        self.run_button["state"] = "normal" if ready else "disabled"

    # -- background work ------------------------------------------------------

    def _measure(self) -> None:
        if self.source_info is None or self.target_info is None:
            return
        self.align_button["state"] = "disabled"
        # Measured on a 223,615-frame remux: four and a half minutes. Saying so
        # is the difference between waiting and wondering.
        self.alignment_text.set("measuring — on a feature this takes a few minutes…")

        def work() -> None:
            try:
                result = align(
                    self.source_info,
                    self.target_info,
                    self.box,
                    progress=lambda message: self.messages.put(("log", message)),
                )
            except Exception as error:
                self.messages.put(("align-error", error))
            else:
                self.messages.put(("align", result))

        threading.Thread(target=work, daemon=True).start()

    def _run(self) -> None:
        if self.plan is None:
            return
        self.run_button["state"] = "disabled"
        self.align_button["state"] = "disabled"
        self.finished = False
        self._log("")

        plan = self.plan
        output = Path(self.output_path.get())
        workdir = Path(self.workdir_path.get())
        approved = self.alignment
        want_pixels = self.verify_pixels.get()

        def work() -> None:
            try:
                result = run(
                    plan,
                    output,
                    workdir=workdir,
                    toolbox=self.box,
                    progress=lambda message: self.messages.put(("log", message)),
                    # The offset was measured and looked at in the window already;
                    # re-measuring it here would decode the same minutes twice.
                    alignment=approved,
                    approve=lambda _: True,
                )
                self.messages.put(("log", "verifying"))
                report = verify(
                    result.output,
                    target=plan.target.path,
                    rpu=result.rpu,
                    hdr10plus=result.hdr10plus,
                    clean_target_stream=result.clean_target_stream if want_pixels else None,
                    workdir=workdir,
                    toolbox=self.box,
                )
                self.messages.put(("verify", report))
                if result.clean_target_stream is not None:
                    result.clean_target_stream.unlink(missing_ok=True)
            except NotEnoughSpace as error:
                self.messages.put(("error", error))
            except Exception as error:
                self.messages.put(("error", error))
            finally:
                self.messages.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        """The message pump, rescheduling itself."""
        self._drain_once()
        self.after(100, self._drain)

    def _drain_once(self) -> None:
        """Empty the queue once and return.

        Split out from the timer loop so a test can pump the window by hand.
        Waiting on the worker thread directly would pass even if this wiring
        were broken, and this wiring is the part most likely to be wrong.
        """
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(payload))
            elif kind == "align":
                self._apply_alignment(payload)  # type: ignore[arg-type]
            elif kind == "align-error":
                self.alignment_text.set("measurement failed")
                self.align_button["state"] = "normal"
                messagebox.showerror("Alignment failed", str(payload))
            elif kind == "verify":
                self._show_report(payload)
            elif kind == "error":
                self._log(f"stopped: {payload}", "fail")
                messagebox.showerror("Stopped", str(payload))
            elif kind == "done":
                self.align_button["state"] = (
                    "normal" if self.plan and self.plan.needs_alignment else "disabled"
                )
                self._refresh_run_button()
                self.finished = True

    def _apply_alignment(self, result: Alignment) -> None:
        self.alignment = result
        self.align_button["state"] = "normal"
        offset = "no offset" if result.offset is None else f"offset {result.offset:+d} frames"
        self.alignment_text.set(
            f"{offset}   ·   confidence {result.confidence:.2f}   ·   "
            f"measured in {len(result.windows)} place(s)   ·   {result.verdict.value}"
        )
        self._log(f"alignment: {result.describe()}")
        self._log(result.reason, "pass" if result.usable else "fail")
        # The override only appears when there is something to override: a
        # measured offset the tool does not trust. A trustworthy result needs no
        # box, and a result with no offset at all has nothing to force.
        needs_override = not result.usable and result.offset is not None
        self.override_check["state"] = "normal" if needs_override else "disabled"
        self.override.set(False)
        self._refresh_run_button()

    def _show_report(self, report) -> None:
        for check in report.checks:
            tag = (
                "pass" if check.passed and not check.skipped else ("" if check.skipped else "fail")
            )
            self._log(check.describe(), tag)
        self._log("")
        if report.passed:
            self._log("All measured checks passed.", "pass")
        else:
            self._log("Verification failed. Do not keep this output.", "fail")

    def _log(self, message: str, tag: str = "") -> None:
        self.log_text["state"] = "normal"
        self.log_text.insert("end", message + "\n", tag or ())
        self.log_text.see("end")
        self.log_text["state"] = "disabled"


def main() -> int:
    root = tk.Tk()
    root.title(f"hibrit {__version__}")
    root.geometry("980x860")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
