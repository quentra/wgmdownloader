# 0012 — Broken links: only what a folder listing links to

Date: 2026-10-25

## Problem
wget's scan follows links inside every HTML file it meets. Archives often hold web pages saved from a browser, whose images (`<page>_files/…`) and links point at files that were never uploaded or were renamed (spaces to underscores). Each of those showed up as a broken link: a user's archive reported about 80, none of them files the server lists.

## Decision
When a scan finishes with broken links, the app fetches the listing of each one's folder (`wget --output-document=-`, one short run per folder, at most `LISTING_LIMIT`) and keeps a link only if that listing links to it. Links are compared by decoded path with repeated slashes collapsed (`url_key`); garbled names are matched in their repaired form too (0010). A folder the server doesn't have (404) lists nothing. A listing that can't be fetched (network error, stop) keeps its links, so nothing is hidden by a failed check. Left-out links are written to the log, and the scan summary counts them. While the check runs, the job still shows as scanning.

Rejected: stopping wget from following links inside pages (`--follow-tags`, regexes) — wget can't tell a listing from any other HTML page, and `--follow-tags=a` still follows `<a>` links inside pages; parsing wget's debug output to learn each link's referring page (huge output, fragile).

## Known limits
Files that exist and are reachable only through a page's link, not through any listing, are still listed and downloaded. Servers whose folder URLs serve a custom index page are judged by that page.
