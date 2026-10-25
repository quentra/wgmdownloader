# 0001 — Initial design

Date: 2026-10-25. Partly superseded by 0002.

## UI: local web page served by a stdlib Python server
Rejected: Tkinter. Tk is absent from many Linux and Homebrew Python builds, and the UI could not be tested headlessly. A browser page works on Windows, macOS and Linux and can be driven by Playwright.
Rejected: Electron or any framework. Adds dependencies and a build step against a simplicity brief.

## Controls are per job, not per file
One job is one wget process. wget cannot pause individual files inside a recursive run, so Stop/Resume act on the whole job.

## Resume = rerun with --continue
Stop sends SIGTERM (TerminateProcess on Windows); Resume reruns the same command. `--continue` resumes the partial file via a Range request and skips complete files (server answers 416). Limitation: servers without Range support make wget refuse to resume a partial file.

## Progress from `--no-verbose` output
One line per saved file gives the count, bytes and path. There is no per-file progress bar; that would require parsing the verbose dot/bar output.

## Size estimate = `wget --spider` with the same flags
Rejected: a separate Python crawler, or reading sizes from listing pages. A separate crawler could disagree with wget about which files match. Listing pages often omit sizes or round them. Spider sends one HEAD request per file and reads Content-Length, so it is accurate but slow on very large trees. Files without Content-Length are reported as "unknown size". Each estimate run starts from zero.

## Default crawl flags
`-r -np --ignore-case -e robots=off --reject-regex [?&]C=[NMSD]`. The reject-regex skips Apache's column-sort links, which would otherwise be crawled as duplicate pages. Downloads add `-nH -c -nv`. Directory-looking URLs without a trailing slash get one, so `--no-parent` stays inside the folder.

## Security
Bound to 127.0.0.1. A per-run token is embedded in the page and required as an X-Token header. A custom header cannot be sent cross-origin without a CORS preflight, which the server never grants. The Host header must be 127.0.0.1:port or localhost:port, which blocks DNS rebinding.
