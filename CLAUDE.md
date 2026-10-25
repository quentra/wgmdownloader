# WGMDownloader (wget-gui)

Minimal local frontend for wget: URL + folder (typed or chosen with the system folder dialog) + file types; jobs with an overall progress bar and per-file rows (name, bar, size, speed, pause/resume/retry); "Compare with folder", which lists remote files missing or incomplete locally, with their sizes, and downloads them; a parallel-downloads stepper.

Architecture: one stdlib-only Python file (`wget_gui.py`) runs a localhost HTTP server with an inline HTML page. Each job scans with `wget --spider` to list files, then each file is fetched by its own `wget --continue` process under a global parallel limit. Released as `wgmdownloader-windows.zip`: a PyInstaller folder build (`wgmdownloader/wgmdownloader.exe` plus `_internal/`, wget bundled), built by GitHub Actions with a pinned PyInstaller whose launcher is compiled from source, and attested.

## Run / test
- Run: `python wget_gui.py` (opens http://127.0.0.1:8765/); `--port N`, `--no-browser`
- Test: `python -m unittest -v test_wget_gui` (needs wget; uses a local HTTP/1.1 fixture server)
- Check which wget is used: `python wget_gui.py --check`
- Build/release: `.github/workflows/build.yml` runs the tests on Ubuntu and Windows, builds on windows-latest, zips with `ci/package.py`, then runs `ci/smoke_test.py` on the unzipped program. To release, bump `VERSION` on main (the workflow publishes `v<VERSION>` if that release doesn't exist yet) or push a tag `vX.Y.Z`. It can't be built locally from Linux (PyInstaller doesn't cross-compile).

## Hard constraints
- Stdlib only; a single file. No frameworks, and no build step for the script itself.
- It must keep working frozen: never run `python -c` or assume `__file__`'s folder holds wget. Use `self_command()` to relaunch, `find_wget()` (next to exe, then `sys._MEIPASS`, then PATH), and `popen()` for every wget (it binds children to the app's job object on Windows).
- Bind to 127.0.0.1 only. Keep the Host-header check and the X-Token check on every API call.
- Parsers depend on English wget output; `wget_env()` forces LC_MESSAGES=C. Don't remove it.
- "Saving to" uses curly quotes in UTF-8 locales and straight quotes otherwise; spider retry lines carry "(try: N)". Both are covered by parser tests.
- Never let wget choose or receive a non-ASCII path (Windows wget uses the ANSI code page for file names and arguments, which garbles Polish, Cyrillic and similar). Downloads go to `<target>/.wgm-partial/<sha1(url)>.part` via `--output-document` with `cwd=` that folder; the app moves the finished file to `_final_path()`. Scans run with `cwd=` the temp folder. Both use `--no-iri` (without it Windows wget double-encodes non-ASCII URLs, so those files look broken). See 0008.
- A broken link whose URL decodes as UTF-8 re-read as Windows-1252 (`unmangle_url`) is not reported: the app scans the corrected URL instead (`job.repair_queue`, follow-up `_start_scan(job, url=...)`), so files are listed and folders scanned under their real names. Only links that still fail after correction are broken. See 0010.
- File types: `parse_types()`. Empty, "all", "any", "everything", "*" or "*.*" mean no filter. Users have typed "all" into this field; never treat it as an extension.
- The parallel limit is global (all jobs), 1–10, default 3. Lowering it never pauses running files.
- Downloads are saved relative to the start URL: folder + `safe_dirs(file.rel)` + `safe_name(url name)`. Those two must keep stripping `.`/`..`, separators and Windows-invalid names; they are the only thing between server paths and the user's disk. A complete file already at its final path is skipped without a request; an incomplete one is moved into the partial file and resumed.
- Scans run with `--no-directories` so temp paths stay short (Windows 260-char limit). A scan that errors after finding files is "done" with a note carrying wget's message; with nothing found it is "failed".
- Folder matching (`_classify`): file at the path a download would use counts as in the folder, or as incomplete if smaller. Otherwise any file under the folder with the same name (case-insensitive) and the same known size counts, which also covers the pre-0005 full-path layout. Same name with a different or unknown size is missing.
- Broken links: `job.broken_links` (url + status), shown via `files?status=broken`; replaced on each scan.
- Antivirus false positives (0011): keep the folder build (no one-file), the source-built launcher and the pinned PyInstaller; the executable never starts PowerShell. Bump the pin deliberately.
- The folder dialog runs on this machine via `pick_folder()` (tkinter on Windows, plus PowerShell when running from source, osascript on macOS, zenity/kdialog/tkinter on Linux). Tests replace `picker_commands`; never open a real dialog in tests.
- Licence: GPL-3.0-or-later (LICENSE, SPDX header in wget_gui.py). Keep example paths and fixtures generic: no names or paths from real archives a user downloaded.
- Simplicity is the brief: add features only on request.
