# 0005 — Short paths, and scans that end with errors

Date: 2026-10-25. Changes the download layout from 0002.

## Problem
A Windows user's scan of a deep archive failed with "could not write the file". wget saves each listing page briefly while scanning, and it recreated the server's folder structure inside the temp folder. One page path came to about 262 characters, over Windows' 260 limit (this wget build reports it as "Bad file descriptor"). wget carried on and finished the scan, but that one error set exit code 3 and the job was marked failed. Downloads had the same exposure, because they recreated the server's full path (`docs/Collection/Section/...`) under the chosen folder.

## Decisions
- **Scans run with `--no-directories`.** Listing pages are saved flat in the temp folder, so neither server depth nor folder names affect the scan.
- **Downloads are saved relative to the start URL.** A file at `<start URL>/CofD/x.pdf` goes to `<folder>/CofD/x.pdf`. wget is called with `--no-directories --directory-prefix <folder>/<relative folders>`, and the app builds those folders itself through `safe_dirs()`. That function replaces `.` and `..` (so a server path can never point outside the folder) and, on Windows, characters and names Windows refuses (`<>:"|?*`, trailing dots or spaces, CON/NUL/COM1 and so on).
- **A scan that errors after finding files counts as done.** The job keeps what was found, and a note quotes wget's first error message (temp paths stripped) and says a few folders may be missing. A scan that errors before finding anything fails, with wget's message in the reason. Exit code 3 reads "couldn't save a page while reading it" for scans, not the download wording about the folder.

## Consequences
- Files downloaded earlier in the full-path layout are still recognised by Compare through the same-name-and-same-size match, and are not downloaded again from a comparison. The plain Download button doesn't look for them, so it would fetch them again into the new layout.
- Paths on disk are shorter by the depth of the start URL, which keeps most archives well under 260 characters. A very long file name inside a long chosen folder can still exceed it; choosing a short folder avoids that.

Rejected: enabling Windows long-path support (needs a registry change and a long-path-aware wget build, and can't be done from this app); keeping the full server path for downloads (it is the main source of long paths, and rarely what people want).
