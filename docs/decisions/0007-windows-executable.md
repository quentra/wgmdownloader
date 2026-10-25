# 0007 — Windows executable and GitHub releases

Date: 2026-10-25

## Decision
The app is distributed as `wgmdownloader.exe`: `wget_gui.py` frozen with PyInstaller (one file, console window), with wget bundled inside. GitHub Actions builds it on `windows-latest`, because PyInstaller can't cross-compile. A release with the exe and `THIRD_PARTY_NOTICES.txt` is published when `VERSION` on main names a version that has no release yet, or when a `v*` tag is pushed. (The VERSION route exists because the environment this was built from couldn't push tags.)

- **wget source:** Chocolatey's `wget` package, which installs the eternallybored.org build and verifies its checksum. The real binary is copied, not Chocolatey's shim. On the test runners the real binary also goes first on PATH: terminating the shim leaves the real wget running, which breaks the pause tests.
- **wget lookup** (`find_wget`): a `wget.exe` next to the program wins, so it can be replaced without a rebuild; then the bundled copy (`sys._MEIPASS`); then PATH.
- **Folder dialog:** `python -c` doesn't exist inside an exe, so the Tk picker runs as the program itself in a hidden `--pick-folder` mode (`self_command`). Tk is bundled because `have_tk()` imports it.
- **Child processes:** on Windows every wget is placed in a job object with KILL_ON_JOB_CLOSE (`bind_to_app`). Closing the console, a crash or ending the task then ends running downloads too, instead of leaving them running unseen.
- **`--check`:** prints which wget will be used and its version. CI uses it, and it helps when diagnosing a user's setup.
- **Checks before publishing:** the full test suite on Ubuntu and Windows, then `ci/smoke_test.py` against the built exe. It runs with PATH cut down to System32 (so only the bundled wget can work), compares against an empty folder, downloads, and verifies the bytes, including a file with an apostrophe and a non-ASCII letter in its folder and name.
- **Notices:** wget is GPL-3.0 and runs as a separate program. The release includes the GPL text, wget's version and links to its source and Windows build, plus the Python licence (which covers the bundled runtime and Tcl/Tk).

Rejected: a console-less (`--windowed`) build, because there would be no visible sign the app is running and no obvious way to quit; Nuitka or other freezers (slower builds, no benefit for a stdlib-only script); downloading wget at first run (needs network access and a second trust decision at run time).

## Known limits
- The exe is unsigned, so Windows SmartScreen warns on first run ("More info", then "Run anyway"). Some antivirus products flag PyInstaller one-file executables generically.
- One-file executables unpack to a temp folder at start, which adds a second or two to start-up and to each folder dialog.
