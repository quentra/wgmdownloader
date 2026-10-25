# 0004 — Broken links list

Date: 2026-10-25

Each scan records every URL wget reports as "Remote file does not exist", with the server's last status line for it (e.g. "404 Not Found"), deduplicated by URL. A rescan replaces the list. The job's "N broken links" stat opens a "Broken links" tab; rows show the path relative to the start URL, link to the URL in a new tab, and show the status. "Copy all URLs" copies the full list, one URL per line.

Why: the log keeps only its latest lines, so it could not show more than about 150 broken links.

Limitation: wget's spider output does not say which page linked to a broken URL, so the referring page is not shown.

Rejected: parsing the "Found N broken links" summary wget prints at the end (only available after the scan finishes, and has no status codes).
