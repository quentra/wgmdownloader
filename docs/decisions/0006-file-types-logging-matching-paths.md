# 0006 — "all" in File types, scan logging, stricter matching, escaped paths

Date: 2026-10-25

## "all" means every type
The empty File types field showed "all" as a placeholder. A user typed "all" into it, and the app passed it to wget as an extension (`--accept all`), so a compare found nothing. Now `parse_types()` treats empty, "all", "any", "everything", "*" and "*.*" as no filter, anywhere in the list. "*.pdf" and ".pdf" mean "pdf", and two-part suffixes such as "tar.gz" are kept (they were previously flattened to "targz"). The placeholder reads "pdf, epub… (empty = all)". A scan that finds nothing because of the filter says so and suggests clearing the field.

## Scan log lists what it found
Each file found adds "Found: <path> (<size>)" to the job log, and each scan ends with a summary line (files, size, broken links, file types). Without them the log couldn't show whether a scan had found nothing or misclassified what it found. The log keeps 2000 lines, and the page shows the last 300.

## Name match needs a known size
Matching a remote file to a same-named local file elsewhere in the folder now requires the server to report a size, and the sizes to agree. A name alone was too loose: an archive with repeated names (`report.pdf` in every year's folder) would show files as present that were not.

## Escaped paths in wget's messages
wget escapes file names in its "Saving to" line. A backslash is always doubled; outside UTF-8 locales (as in the Windows build) `'` becomes `\'` and non-ASCII bytes become octal codes. The app used the escaped text as a path, so progress stayed at 0% for such files. `wget_unescape()` decodes it, and progress falls back to the path the app expects (`f.expect`) if the parsed path still doesn't exist.
