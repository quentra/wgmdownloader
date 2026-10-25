# 0008 — ASCII partial files and --no-iri (Windows non-ASCII names)

Date: 2026-10-25. Changes how files are written (0002, 0005).

## Problem
The first Windows CI run failed on a fixture named `ü file.pdf`. A probe on a Windows runner (wget 1.21.4, the eternallybored build Chocolatey installs) showed two separate faults:
- **Requests:** by default wget re-encodes non-ASCII letters in links through the ANSI code page. `%C3%BC` (ü) was requested as `%C3%83%C2%BC`, so the server answered 404 and the file showed as a broken link; a folder named `Zażółć` vanished from the scan altogether. `--no-iri`, `--local-encoding=UTF-8` and `--remote-encoding=UTF-8` all fixed the requests.
- **File names:** with every option, names outside the ANSI code page were written garbled (`gęślą jaźń.pdf` → `gÄ™Å›lÄ… jaÅºÅ„.pdf`); with the UTF-8 options even `ü` was garbled. wget's Windows build uses the ANSI file APIs, so a non-ASCII `--directory-prefix` would fail the same way.

## Decision
- Scans and downloads use `--no-iri`, so URLs are requested exactly as the server wrote them.
- wget never sees a non-ASCII path. Each download goes to `<target>/.wgm-partial/<sha1 of URL>.part`, passed as a bare `--output-document` name with `cwd=` set to that folder (Python passes the working folder through the Unicode API). The app then moves the finished file to its real name with `os.replace`. The scan's temp folder is passed the same way (`cwd=`).
- The `.part` name comes from the URL, so a paused or interrupted download resumes into the same file (`--continue` with `-O` sends a Range request, as tested). An incomplete file found at the final path (from earlier versions or other tools) is moved into the partial file and resumed. A complete one is skipped without contacting the server.
- Progress is the partial file's size, so parsing wget's "Saving to" line (and undoing its escaping) is gone.
- After a failure an empty partial (wget creates it even on a 404) is deleted. The `.wgm-partial` folder is removed once empty and no download into that target is running.

## Consequences
- Unfinished data is in `.wgm-partial`, not next to the finished files, while downloading.
- File names are sanitised by the app (`safe_name`), not by wget's `--restrict-file-names`. On Windows a name with a character Windows refuses gets `_` in its place.
- This may explain broken links reported on Windows before this change, if those files had non-ASCII names.
