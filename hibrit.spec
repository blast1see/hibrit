# PyInstaller build. Run with: pyinstaller --noconfirm hibrit.spec
#
# Two executables from one collection: hibrit.exe is a console program and
# hibrit-gui.exe is not, so double-clicking the window does not leave a black
# terminal behind it. They share every bundled library, which is why this is one
# spec rather than two.
#
# **The external tools are deliberately not bundled.** dovi_tool and
# hdr10plus_tool are separate GPL programs under active development, and
# freezing a copy of each would mean shipping a snapshot that silently goes
# stale — the version that mattered here (dovi_tool 2.3.3) reads profile 8.1
# streams a 2021 build does not. `hibrit doctor` names what is missing and where
# to get it, which ages better than a bundled binary does.
#
# One directory rather than one file: a onefile build unpacks itself into a
# temporary directory on every launch, and this program is often started twice
# in a row to look at a plan before running it.

from pathlib import Path

ROOT = Path(SPECPATH)

#: Test machinery has no business in a release, and numpy's own test suite is
#: a third of its size on disk.
EXCLUDES = ["pytest", "ruff", "numpy.testing", "numpy.f2py"]

# There is no tcl/tk data directory in the output and there should not be one.
# Searching the bundle for init.tcl finds nothing, which looks like a build
# about to fail on first launch — it is not. Tcl 9 mounts its script library
# inside the DLL and reports it as "//zipfs:/lib/tcl/tcl_library". Both
# executables were built and launched to check; both open a window. Do not
# "fix" this by collecting tcl data files.

cli = Analysis(
    [str(ROOT / "packaging" / "entry_cli.py")],
    pathex=[str(ROOT)],
    hiddenimports=["hibrit.gui"],
    excludes=EXCLUDES,
    noarchive=False,
)

gui = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT)],
    hiddenimports=["hibrit.gui"],
    excludes=EXCLUDES,
    noarchive=False,
)

MERGE((cli, "hibrit", "hibrit"), (gui, "hibrit-gui", "hibrit-gui"))

cli_pyz = PYZ(cli.pure, cli.zipped_data)
gui_pyz = PYZ(gui.pure, gui.zipped_data)

cli_exe = EXE(
    cli_pyz,
    cli.scripts,
    [],
    exclude_binaries=True,
    name="hibrit",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

gui_exe = EXE(
    gui_pyz,
    gui.scripts,
    [],
    exclude_binaries=True,
    name="hibrit-gui",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

COLLECT(
    cli_exe,
    cli.binaries,
    cli.zipfiles,
    cli.datas,
    gui_exe,
    gui.binaries,
    gui.zipfiles,
    gui.datas,
    strip=False,
    upx=False,
    name="hibrit",
)
