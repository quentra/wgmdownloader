# 0013 — HTML pages are files

Date: 2026-10-25

## Problem
wget's scan reads every HTML response for links ("Remote file exists and could contain links to other resources -- retrieving.") and never reports it the way it reports other files ("does not contain any link"). So `.htm`/`.html` files in an archive were never listed or downloaded, even with no file-type filter. This dated from the first version.

## Decision
On that line, the app lists the URL as a file when `is_page_file` says so: the path has no trailing slash (a folder listing) and no query (a generated page), and the name matches the file types, applied here because wget's `--accept` doesn't stop it reading pages.

Rejected: telling HTML apart by Content-Type alone (folder listings are text/html too).
