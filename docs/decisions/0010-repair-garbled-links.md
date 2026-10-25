# 0010 — Repair links that wget garbles

Date: 2026-10-25

## Problem
On a user's Windows machine, v1.0.0 reported every link with a non-ASCII name as broken (404). wget had requested each name's UTF-8 bytes re-read as Windows-1252 and encoded again: `Genevi%C3%A8ve` became `Genevi%C3%83%C2%A8ve`, `It’s` became `Itâ€™s`, `文件` became `æ–‡ä»¶`. A probe on the Windows CI runner showed the bundled wget with `--no-iri` requesting raw-UTF-8 links correctly (with no charset, a UTF-8 header, or a UTF-8 meta tag), and wget's default mode producing exactly the user's garbling. So the user's setup made wget behave as if `--no-iri` were off. The most likely cause is a different wget.exe placed next to the program, which `find_wget` prefers, but it is unconfirmed.

## Decision
When a scan reports a broken link, `unmangle_url` checks each path segment. If the decoded text maps back to bytes through Windows-1252 (its five undefined bytes taken as the C1 controls Windows maps them to), and those bytes are valid UTF-8 that differ from the text, the segment is replaced with the correctly percent-encoded name. If any segment changed, the link isn't reported. The app runs a follow-up wget scan of the corrected URL with the same options and file-type filter. A file is listed with its size; a folder is scanned in turn, and its own garbled links are repaired the same way. Follow-up scans run one after another after the main scan, and the job shows as scanning until they finish. Links that still fail after correction are reported under their corrected, readable names. Up to 2000 corrections per scan.

This works whatever causes the garbling, because it corrects the URL the server needs. Correct names are left alone: Windows-1252 bytes of real non-ASCII text are almost never valid UTF-8.

## Tests
A fixture whose links are already garbled this way, so real wget on any OS takes the repair path: the files are found and downloaded under their real names, a repaired folder (named 文件) is scanned including a Polish name inside it, and a link broken even after correction stays broken. A second fixture with raw-UTF-8 links checks the normal path on Linux and Windows.

Rejected: switching to `--local-encoding=UTF-8 --remote-encoding=UTF-8` (the probe showed no advantage over `--no-iri` on the runner, and the user's failure isn't explained by flags); writing a separate Python crawler (duplicates wget's recursion and filter rules).
