# 0002 — Scan first, then one wget per file

Date: 2026-10-25. Supersedes the "controls are per job", "resume = rerun with --continue" and "progress from --no-verbose output" sections of 0001.

## Decision
A job runs `wget --spider` over the URL to list matching files with their Content-Length. Each file found is queued and fetched by its own `wget --continue --force-directories --no-host-directories` process. A global scheduler keeps at most N files downloading across all jobs. Downloads start as soon as the first files are listed; the scan does not have to finish first.

Chosen to match established mass downloaders (JDownloader, DownThemAll, Motrix): a row per file with name, bar, size, speed and its own pause/resume, a combined bar per job, and a settable number of simultaneous downloads.

Rejected: keeping one recursive wget per job and adding a bar for the current file. That gives no overall bar without a prior estimate, no per-file control, and no parallelism.

## Consequences
- Paths match the old recursive layout, because `-x -nH` derives directories from the URL path the same way.
- Progress comes from the size of the file on disk (path taken from wget's "Saving to" line), not from parsing wget's progress bar. Speed is a smoothed rate of change of that size.
- Pause terminates that file's wget. Resume requeues it, and `--continue` sends a Range request from the partial size (verified in tests).
- Pausing a job also stops its scan. Resuming restarts the scan and merges by URL, so nothing is listed twice. "Check for new files" uses the same merge.
- Cost: one HEAD per file during the scan, plus one GET. The scan runs alongside downloads and does not count against the limit.
- `--level=inf` on the scan, because wget's default recursion depth is 5.
- A pause or failure keeps the file visible under the collapsed job (up to five of each), so its resume or retry button stays in view.

## Parallel downloads control
A −/+ stepper with a typed number input in a sticky footer beside total speed, where JDownloader puts its spinner. Range 1–10, default 3, applied immediately. Lowering it lets running files finish rather than pausing them. Values are clamped on the server.
Rejected: a dropdown (one extra click to change by one); a slider (imprecise for small integers, poor keyboard and screen-reader behaviour); a per-job setting (download managers use a global limit, and per-job limits multiply load on one server).
