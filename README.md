# WGMDownloader

WGMDownloader is a minimalist, Claude-authored wget mass downloader tool. No installation necessary. Plug in the target URL, the target folder, and you're off to the races.

Other features include:

- **Compare with folder**: scans the URL and lists every file that is missing or incomplete in your folder, with sizes and the total left to download. Download all of them, or pick single files.
- **Per-file progress**: a bar, size, speed and status for every file, plus an overall bar with time left for each job.
- **Pause, resume and retry**: for single files or whole jobs. Interrupted files continue where they stopped.
- **Parallel downloads**: 1 to 10 files at once, adjustable while downloads run.
- **File-type filter**: e.g. `pdf, epub`; leave it empty for everything.
- **Broken-link report**: every dead link with the server's response, copyable as a list.
- **Check for new files**: picks up files added to the server since the last run.
- **Folder structure kept**: files are saved under your folder in the same subfolders as on the server, starting from the URL you entered.
- **Browse button**: choose the target folder with your system's folder window.
- **Self-contained**: a single executable with wget built in. The interface opens in your browser and is only reachable from your own computer.
- **Light and dark mode**: follows your system setting.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). Written by Claude (Anthropic). The Windows executable bundles GNU Wget, also GPL-3.0; its notices ship with each release.

Questions? Feedback? Mail-to [john.bestbpm1@gmail.com](mailto:john.bestbpm1@gmail.com).
