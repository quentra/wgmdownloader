# 0003 — Compare with folder, and a native folder picker

Date: 2026-10-25. Replaces the "Estimate size" action from 0001/0002.

## Compare with folder
A compare job runs the same spider scan as a download, then checks each listed file against the chosen folder:
- **In folder**: a file exists at the path a download would write (folder + URL path, matching `-x -nH`), at least as large as the remote file. Or a file with the same name (case-insensitive) and the same size exists anywhere under the folder, which covers copies kept in a flat or reorganised folder.
- **Incomplete**: a file exists at the download path but is smaller. Downloading it resumes with `--continue`.
- **Missing**: everything else, including same-name files of a different size.

"Download N files" turns the job into a download of the missing and incomplete files only. Each missing row also has its own download button. "Compare again" rebuilds the folder index, rechecks every listed file, and rescans the URL for new files.

Replaces "Estimate size": a comparison shows the same totals (file count and size online), and against an empty or non-existent folder every file counts as missing, which is the old estimate.

Rejected: matching by name only (two different remote files with the same name, such as yearly `report.pdf`, would hide each other); hashing (needs every remote file downloaded first); comparing modification times (open directories often report none, and copies lose them).

## Folder picker
Browsers do not expose a folder's path to a page (`showDirectoryPicker` and `webkitdirectory` return handles or file lists, not paths), and wget needs a path. The local server therefore opens the operating system's own dialog and returns the path. In order of preference:
- Windows: tkinter's askdirectory (the modern Explorer dialog), then PowerShell's FolderBrowserDialog.
- macOS: osascript `choose folder`, then tkinter.
- Linux: zenity, kdialog, then tkinter.

The field stays editable. If no dialog works, the API returns 501 and the page asks for a typed path. Only one dialog can be open at a time.
