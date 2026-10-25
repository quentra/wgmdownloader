#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 WGMDownloader contributors. Written by Claude (Anthropic).
"""Minimal local web frontend for wget.

Run:  python wget_gui.py            (opens http://127.0.0.1:8765/ in your browser)
      python wget_gui.py --port 9000 --no-browser

Needs Python 3.8+ and wget on PATH (or wget.exe next to this file). No other dependencies.
The Windows release (wgmdownloader.exe) is this file frozen with PyInstaller, with wget bundled.

How it works: each job first scans the URL with `wget --spider` to list matching files and
their sizes. Every file found is queued and fetched by its own `wget --continue` process, with
at most N files downloading at once across all jobs (N is set in the page).
"Compare with folder" scans the same way, then checks each file against the local folder and
lists what is missing, incomplete or already there.
"""

import argparse
import collections
import hashlib
import itertools
import json
import locale
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 10, 3

WGET_EXIT = {
    1: "wget error",
    2: "bad wget option",
    3: "could not write the file (check the folder)",
    4: "network failure",
    5: "SSL verification failed",
    6: "authentication required",
    7: "protocol error",
    8: "server returned an error",
}

# Spider output (verbose mode)
# On a retry wget writes "--<date>-- (try: 2)  <url>".
SPIDER_URL_RE = re.compile(r"^--\d{4}-\d\d-\d\d \d\d:\d\d:\d\d--\s+(?:\(try:\s*\d+\)\s+)?(\S+)")
SPIDER_LEN_RE = re.compile(r"^Length:\s+(\d+|unspecified)")
SPIDER_STATUS_RE = re.compile(r"awaiting response\.\.\.\s*(\d{3})\s*(.*?)\s*$")
HTTP_ERROR_RE = re.compile(r"ERROR (\d{3}):\s*(.*?)\.?\s*$")
PROGRESS_LINE_RE = re.compile(r"^\s*\d+[KMG]\s+[.,\s]+")

# --level=inf: wget's default recursion depth is 5. The reject-regex skips Apache's column-sort
# links (?C=N;O=D), which would otherwise be crawled as duplicate pages.
# --no-directories: the scan saves each listing page briefly to parse its links. Kept flat in
# the temp folder, a deep server path can't push it past Windows' 260-character limit.
#
# --no-iri: Windows builds of wget otherwise re-encode non-ASCII letters in links through the
# ANSI code page (ü, sent as %C3%BC, becomes %C3%83%C2%BC), so those files look missing.
SCAN_ARGS = ["--spider", "--recursive", "--level=inf", "--no-parent", "--ignore-case",
             "--no-directories", "--no-iri", "-e", "robots=off", "--reject-regex", r"[?&]C=[NMSD]"]
# wget only ever writes an ASCII-named partial file in its working folder; the app gives it its
# real name when it's complete. Windows wget writes names through the ANSI code page, which
# garbles anything outside it (Polish, Cyrillic, ...), and can't take such paths as arguments.
FILE_ARGS = ["--continue", "--no-iri", "--progress=dot:mega", "-e", "robots=off"]
PARTIAL_DIR = ".wgm-partial"      # inside the target folder; removed when empty
# The scan follows links inside every HTML file it meets, including web pages saved into an
# archive, whose images and links often point at files that were never uploaded. A broken
# link is only reported if its folder's listing links to it (see 0012); fetching a listing
# takes one short wget run per folder, up to this many folders.
LISTING_ARGS = ["--quiet", "--no-iri", "--timeout=30", "--tries=2", "-e", "robots=off",
                "--output-document=-"]
LISTING_LIMIT = 500
SCAN_EXIT = {**WGET_EXIT, 3: "couldn't save a page while reading it"}
# wget lines that explain why a scan ended with an error (404s are broken links, not errors)
SCAN_ERROR_RE = re.compile(r"^(Cannot write to .*|.*failed: .*|Unable to establish SSL.*|"
                           r"ERROR(?! \d{3}).*|.*Authentication Failed.*)$")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} \
    | {"LPT%d" % i for i in range(1, 10)}
SCAN_LOG_SKIP = ("Spider mode enabled", "Connecting to", "HTTP request sent", "Removing ",
                 "unlink:", "Saving to:", "Remote file exists", "Length:")


def wget_env():
    """Force English messages (the parsers depend on them) without changing the charset."""
    env = os.environ.copy()
    if "LC_ALL" in env:
        val = env.pop("LC_ALL")
        env.setdefault("LANG", val)
        env.setdefault("LC_CTYPE", val)
    env["LC_MESSAGES"] = "C"
    env["LANGUAGE"] = ""
    return env


FROZEN = getattr(sys, "frozen", False)      # running as the PyInstaller executable


def self_command(*args):
    """Command line that runs this program again: the .exe itself, or python + this file."""
    return [sys.executable, *args] if FROZEN else [sys.executable, os.path.abspath(__file__), *args]


_kill_job = None


def bind_to_app(proc):
    """Windows: put a child process in a job object that Windows closes when this program
    exits, however it exits (window closed, crash, Task Manager), which ends the child too.
    Without it, closing the console would leave wget downloads running in the background.
    (Elsewhere, Ctrl+C or closing the terminal already reaches the whole process group.)"""
    global _kill_job
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _kill_job is None:
            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

            class BasicLimits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            job = k32.CreateJobObjectW(None, None)
            info = ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return                                              # 9 = JobObjectExtendedLimitInformation
            _kill_job = job
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject(_kill_job, wintypes.HANDLE(int(proc._handle)))
    except Exception:
        pass                                                        # best effort: never block a download


def popen(cmd, cwd=None, stdout=subprocess.DEVNULL):
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout,
                            stderr=subprocess.PIPE, env=wget_env(), cwd=cwd, **kwargs)
    bind_to_app(proc)
    return proc


def lines(stream):
    try:
        for raw in stream:
            yield raw.decode("utf-8", "replace").rstrip("\r\n")
    finally:
        stream.close()


def terminate(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        t = threading.Timer(5, lambda: proc.poll() is None and proc.kill())
        t.daemon = True
        t.start()


def normalize_url(url):
    """Add a trailing slash to directory-looking URLs so --no-parent stays inside them."""
    p = urlparse(url)
    last = p.path.rsplit("/", 1)[-1]
    if p.path and not p.path.endswith("/") and "." not in last and not p.query:
        url = url.replace(p.path, p.path + "/", 1)
    elif not p.path:
        url += "/"
    return url


ALL_TYPES = {"all", "any", "everything", "*"}


def parse_types(text):
    """File-type filter from the form. Empty, "all", "*" and the like mean every type."""
    out = []
    for raw in re.split(r"[\s,;]+", (text or "").strip().lower()):
        if not raw:
            continue
        if raw in ALL_TYPES or raw.strip("*.") == "":    # "all", "*", "*.*"
            return []
        token = raw.lstrip("*").lstrip(".")               # "*.pdf" and ".pdf" mean "pdf"
        if not re.fullmatch(r"[a-z0-9]{1,10}(\.[a-z0-9]{1,10})?", token):
            raise ValueError("File types should look like: pdf, epub, zip. Leave the field empty for every type.")
        if token not in out:
            out.append(token)
    return out


# Windows-1252 leaves these five bytes undefined; Windows maps them to the matching C1 controls.
_CP1252_UNDEFINED = {0x81, 0x8D, 0x8F, 0x90, 0x9D}
REPAIR_LIMIT = 2000      # corrected-URL retries per scan, a guard against pathological listings


def _cp1252_bytes(text):
    out = bytearray()
    for ch in text:
        o = ord(ch)
        if o < 0x80 or o in _CP1252_UNDEFINED:
            out.append(o)
        else:
            try:
                out += ch.encode("cp1252")
            except UnicodeEncodeError:
                return None
    return bytes(out)


def unmangle_url(url):
    """Windows wget can request a link's UTF-8 name re-read as Windows-1252 and encoded again
    (è → Ã¨, ’ → â€™), which the server answers with 404. Return the URL with such path
    segments put right, or None if nothing in it looks garbled that way."""
    p = urlparse(url)
    changed = False
    segments = []
    for seg in p.path.split("/"):
        text = unquote(seg)
        raw = _cp1252_bytes(text)
        fixed = None
        if raw is not None and any(b >= 0x80 for b in raw):
            try:
                fixed = raw.decode("utf-8")
            except UnicodeDecodeError:
                fixed = None
        if fixed and fixed != text:
            segments.append(quote(fixed, safe="!$&'()*+,;=:@~"))
            changed = True
        else:
            segments.append(seg)
    return urlunparse(p._replace(path="/".join(segments))) if changed else None


class _Hrefs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)


def url_key(url):
    """What two links to the same file have in common: the decoded path, single slashes."""
    return re.sub(r"/+", "/", unquote(urlparse(url).path))


def listing_keys(page_url, html):
    """The url_key of every <a href> on a folder listing page (and of its repaired form)."""
    p = _Hrefs()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    keys = set()
    for h in p.hrefs:
        u = urljoin(page_url, h)
        keys.add(url_key(u))
        fixed = unmangle_url(u)           # a garbled link that was scanned under its real name
        if fixed:
            keys.add(url_key(fixed))
    return keys


def parent_url(url):
    """The folder a link sits in: its URL up to the last slash (a folder's own slash excluded)."""
    p = urlparse(url)
    path = p.path[:-1] if p.path.endswith("/") else p.path
    return urlunparse(p._replace(path=path.rsplit("/", 1)[0] + "/", params="", query="", fragment=""))


def fmt_size(n):
    if n is None:
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def safe_dirs(rel, windows=os.name == "nt"):
    """Folder names for a file's path relative to the start URL, safe to create under the
    target: no "." or ".." (which could point outside it) and, on Windows, no characters or
    names Windows refuses."""
    out = []
    for part in rel.split("/")[:-1]:
        if not part:
            continue
        if part in (".", ".."):
            part = "_"
        elif windows:
            part = re.sub(r'[<>:"|?*\\\x00-\x1f]', "_", part).rstrip(" .") or "_"
            if part.split(".")[0].upper() in WINDOWS_RESERVED:
                part += "_"
        else:
            part = part.replace("\x00", "_")
        out.append(part)
    return out


def safe_name(name, windows=os.name == "nt"):
    """A file name from a URL, safe to create: on Windows without the characters and names
    Windows refuses; elsewhere without path separators."""
    if windows:
        name = re.sub(r'[<>:"/|?*\\\x00-\x1f]', "_", name).rstrip(" .") or "_"
        if name.split(".")[0].upper() in WINDOWS_RESERVED:
            name = "_" + name
    else:
        name = name.replace("/", "_").replace("\x00", "_") or "_"
        if name in (".", ".."):
            name = "_"
    return name


def build_index(root):
    """Map lower-cased file name -> [(path, size)] for every file under root."""
    index = {}
    if not os.path.isdir(root):
        return False, index
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != PARTIAL_DIR]
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                index.setdefault(name.lower(), []).append((path, os.path.getsize(path)))
            except OSError:
                pass
    return True, index


# ---- native folder dialog (runs on this machine; the page can't get a path from the browser)

def have_tk():
    try:
        import tkinter  # noqa: F401  (also makes PyInstaller bundle Tk)
        return True
    except ImportError:
        return False


def run_tk_picker(initial):
    """The --pick-folder mode: show Tk's folder dialog (the Explorer one on Windows), print
    the choice. Runs in its own process because Tk wants its own main thread."""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tkinter.TclError:
        pass
    path = filedialog.askdirectory(initialdir=initial or None, title="Choose a folder", mustexist=False)
    root.destroy()
    sys.stdout.buffer.write((path or "").encode("utf-8"))
    sys.stdout.flush()


PICKER_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Choose a folder"
$d.ShowNewFolderButton = $true
if ($env:WGET_GUI_INITIAL -and (Test-Path -LiteralPath $env:WGET_GUI_INITIAL)) { $d.SelectedPath = $env:WGET_GUI_INITIAL }
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }
"""

PICKER_OSA = r"""
set initialPath to system attribute "WGET_GUI_INITIAL"
tell current application
    activate
    try
        set f to choose folder with prompt "Choose a folder" default location (POSIX file initialPath as alias)
    on error number n
        if n is -128 then error number -128
        set f to choose folder with prompt "Choose a folder"
    end try
end tell
POSIX path of f
"""


def picker_commands(initial):
    """Folder dialogs to try, best first: (argv, stdin text, exit code that means Cancel)."""
    cmds = []
    tk = (self_command("--pick-folder", initial), None, None) if have_tk() else None
    if os.name == "nt":
        if tk:
            cmds.append(tk)            # modern Explorer dialog
        # PowerShell is only a fallback for running from source without Tk. The executable
        # bundles Tk, and antivirus models score programs that start PowerShell as
        # suspicious, so the executable never starts it (see 0011).
        ps = None if FROZEN else shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            cmds.append(([ps, "-NoProfile", "-NonInteractive", "-STA", "-Command", PICKER_PS], None, None))
    elif sys.platform == "darwin":
        if shutil.which("osascript"):
            cmds.append((["osascript", "-"], PICKER_OSA, 1))
        if tk:
            cmds.append(tk)
    else:
        start = initial if os.path.isdir(initial) else os.path.expanduser("~")
        if shutil.which("zenity"):
            cmds.append((["zenity", "--file-selection", "--directory", "--title=Choose a folder",
                          "--filename=" + os.path.join(start, "")], None, 1))
        if shutil.which("kdialog"):
            cmds.append((["kdialog", "--getexistingdirectory", start, "--title", "Choose a folder"], None, 1))
        if tk:
            cmds.append(tk)
    return cmds


def pick_folder(initial=""):
    """Show a native folder dialog. Returns the path, "" if cancelled, None if no dialog works."""
    initial = os.path.abspath(os.path.expanduser(initial)) if initial.strip() else ""
    env = dict(os.environ, WGET_GUI_INITIAL=initial)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for argv, stdin, cancel_rc in picker_commands(initial):
        try:
            r = subprocess.run(argv, input=(stdin or "").encode("utf-8"), capture_output=True,
                               env=env, **kwargs)
        except OSError:
            continue
        out = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode == 0:
            return os.path.normpath(out) if out else ""
        if cancel_rc is not None and r.returncode == cancel_rc:
            return ""
        # the tool exists but failed (no display, broken install): try the next one
    return None


class FileItem:
    def __init__(self, job, fid, url, size):
        self.job = job
        self.id = fid
        self.url = url
        self.size = size              # bytes, or None if the server sent no length
        self.rel = job.relative(url)
        self.status = "queued"        # queued | active | paused | done | failed, and for a
        #                               folder comparison: missing | partial | local
        self.local = ""               # where a name match was found, relative to the folder
        self.done = 0
        self.speed = 0.0
        self.error = ""
        self.proc = None
        self.dest = None              # the partial file wget writes to while active
        self.pause_requested = False
        self.mark = None              # (time, bytes) for speed calculation

    def view(self):
        d, _, name = self.rel.rpartition("/")
        return {"id": self.id, "name": name, "dir": d + "/" if d else "", "path": self.rel,
                "url": self.url, "size": self.size, "status": self.status, "done": self.done,
                "speed": round(self.speed), "error": self.error, "local": self.local}


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind, url, target, types):
        self.id = next(Job._ids)
        self.kind = kind              # "download" or "compare"
        self.check_local = kind == "compare"   # stays True after "Download missing"
        self.index = {}
        self.folder_exists = None
        self.url = url
        self.target = target
        self.types = types
        self.files = []
        self.by_url = {}
        self.cursor = 0               # first index that may still be queued
        self.paused = False
        self.scan_proc = None
        self.scan_state = "idle"      # scanning | done | stopped | failed
        self.scan_stop_requested = False
        self.scan_reason = ""
        self.broken = 0
        self.broken_links = []        # [{"url", "status"}] from the latest scan
        self.broken_seen = set()
        self.scan_errors = []         # wget's messages when a scan ends with an error
        self.repair_queue = []        # corrected URLs still to scan (see unmangle_url)
        self.repair_tried = set()
        self.scan_gen = 0             # bumped by each new scan; stale listing checks are dropped
        self.log = collections.deque(maxlen=2000)
        path = urlparse(url).path or "/"
        self.base = unquote(path if path.endswith("/") else path.rsplit("/", 1)[0] + "/")

    def relative(self, url):
        path = unquote(urlparse(url).path)
        if path.startswith(self.base) and len(path) > len(self.base):
            return path[len(self.base):]
        return path.lstrip("/")

    def requeue_from(self, index):
        self.cursor = min(self.cursor, index)


class App:
    def __init__(self, wget, default_target):
        self.wget = wget
        self.default_target = default_target
        self.token = secrets.token_urlsafe(18)
        self.allowed_hosts = set()
        self.lock = threading.RLock()
        self.limit = LIMIT_DEFAULT
        self.jobs = []
        self.active = []
        self.wake = threading.Event()
        self.picker_lock = threading.Lock()
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- jobs ----

    def add_job(self, kind, url, target, types):
        url = (url or "").strip()
        p = urlparse(url)
        if p.scheme not in ("http", "https", "ftp") or not p.netloc:
            raise ValueError("Enter a full http://, https:// or ftp:// URL.")
        url = normalize_url(url)
        target = os.path.abspath(os.path.expanduser((target or "").strip() or self.default_target))
        types = parse_types(types)
        job = Job(kind, url, target, types)
        with self.lock:
            try:
                self._start_scan(job)
            except OSError as e:
                raise ValueError("Could not start wget: %s" % e)
            self.jobs.append(job)
        return job

    def get(self, job_id):
        with self.lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def job_action(self, job, action):
        with self.lock:
            if action == "pause":
                job.paused = True
                self._stop_scan(job)
                for f in job.files:
                    if f.status == "queued":
                        f.status = "paused"
                    elif f.status == "active":
                        self._pause_active(f)
            elif action == "resume":
                job.paused = False
                for f in job.files:
                    if f.status == "paused":
                        f.status = "queued"
                job.requeue_from(0)
                if job.scan_state in ("stopped", "failed"):
                    self._start_scan(job)
            elif action == "rescan":
                job.paused = False
                self._start_scan(job)
            elif action == "retry":
                for f in job.files:
                    if f.status == "failed":
                        f.status, f.error = "queued", ""
                job.requeue_from(0)
            elif action == "download":
                job.kind = "download"
                job.paused = False
                for f in job.files:
                    if f.status in ("missing", "partial"):
                        f.status = "queued"
                job.requeue_from(0)
                if job.scan_state in ("stopped", "failed"):
                    self._start_scan(job)
            elif action == "remove":
                self._stop_scan(job)
                for f in job.files:
                    if f.status == "active":
                        self._pause_active(f)
                self.jobs.remove(job)
        self.wake.set()

    def file_action(self, job, fid, action):
        with self.lock:
            if not 0 <= fid < len(job.files):
                return False
            f = job.files[fid]
            if action == "pause":
                if f.status == "queued":
                    f.status = "paused"
                elif f.status == "active":
                    self._pause_active(f)
            elif action == "resume" and f.status in ("paused", "failed", "missing", "partial"):
                f.status, f.error = "queued", ""
                job.paused = False
                job.requeue_from(f.id)
        self.wake.set()
        return True

    def set_limit(self, value):
        with self.lock:
            self.limit = max(LIMIT_MIN, min(LIMIT_MAX, int(value)))
        self.wake.set()
        return self.limit

    def shutdown(self):
        self._running = False
        with self.lock:
            for job in self.jobs:
                self._stop_scan(job)
            for f in list(self.active):
                self._pause_active(f)

    # ---- scanning ----

    def _start_scan(self, job, url=None):
        """Scan the job's URL; with `url`, a follow-up scan of a corrected link that adds to
        what the job already found."""
        if job.scan_proc and job.scan_proc.poll() is None:
            return
        primary = url is None
        tmp = tempfile.mkdtemp(prefix="wget-gui-scan-")
        cmd = [self.wget] + SCAN_ARGS
        if job.types:
            cmd += ["--accept", ",".join(job.types)]
        cmd.append(url or job.url)
        try:
            proc = popen(cmd, cwd=tmp)        # pages land in tmp without passing its path to wget
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        job.log.append("$ " + subprocess.list2cmdline(cmd))
        job.scan_proc = proc
        job.scan_state = "scanning"
        job.scan_stop_requested = False
        if primary:
            job.scan_gen += 1
            job.scan_reason = ""
            job.broken = 0
            job.broken_links = []
            job.broken_seen = set()
            job.scan_errors = []
            job.repair_queue = []
            job.repair_tried = set()
        threading.Thread(target=self._scan_reader, args=(job, proc, tmp, primary), daemon=True).start()

    def _stop_scan(self, job):
        if job.scan_proc and job.scan_proc.poll() is None:
            job.scan_stop_requested = True
            terminate(job.scan_proc)

    def _scan_reader(self, job, proc, tmp, primary=True):
        if primary and job.check_local:
            exists, index = build_index(job.target)
            with self.lock:
                job.folder_exists, job.index = exists, index
                for f in job.files:               # "Compare again": recheck what was listed
                    if f.status in ("missing", "partial", "local"):
                        self._classify(f)
        cur_url, cur_len, cur_status = None, None, ""
        for line in lines(proc.stderr):
            if not line.strip():
                continue
            m = SPIDER_URL_RE.match(line)
            if m:
                cur_url, cur_len, cur_status = m.group(1), None, ""
                continue
            m = SPIDER_STATUS_RE.search(line)
            if m:
                cur_status = ("%s %s" % (m.group(1), m.group(2))).strip()
                continue
            m = SPIDER_LEN_RE.match(line)
            if m:
                cur_len = None if m.group(1) == "unspecified" else int(m.group(1))
                continue
            with self.lock:
                if "does not contain any link" in line:
                    self._add_file(job, cur_url, cur_len)
                elif "Remote file does not exist" in line:
                    fixed = unmangle_url(cur_url) if cur_url else None
                    if fixed and fixed not in job.repair_tried and len(job.repair_tried) < REPAIR_LIMIT:
                        job.repair_tried.add(fixed)
                        job.repair_queue.append((fixed, cur_url, cur_status))
                        job.log.append("Retrying with the name corrected: %s" % fixed)
                    elif cur_url and cur_url not in job.broken_seen:
                        job.broken_seen.add(cur_url)
                        job.broken_links.append({"url": cur_url, "status": cur_status})
                        job.broken = len(job.broken_links)
                        job.log.append("Broken link: %s (%s)" % (cur_url, cur_status or "no response"))
                elif SCAN_ERROR_RE.match(line.strip()):
                    job.log.append(line)
                    msg = line.strip()
                    for prefix in (tmp + os.sep, tmp.replace("\\", "/") + "/"):
                        msg = msg.replace(prefix, "")
                    if msg not in job.scan_errors and len(job.scan_errors) < 50:
                        job.scan_errors.append(msg)
                elif not line.startswith(SCAN_LOG_SKIP) and not PROGRESS_LINE_RE.match(line):
                    job.log.append(line)
        rc = proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
        check = False
        with self.lock:
            if job.scan_proc is proc:
                if job.scan_stop_requested:
                    job.scan_state = "stopped"
                    job.repair_queue = []
                elif rc in (0, 8):        # 8 = some broken links, still a complete scan
                    job.scan_state = "done"
                elif job.files or not primary:
                    # wget carried on past the error; what it found stands, flagged as possibly incomplete
                    job.scan_state = "done"
                else:
                    job.scan_state = "failed"
                    why = SCAN_EXIT.get(rc, "wget exit %d" % rc)
                    if job.scan_errors:
                        why += " (%s)" % job.scan_errors[-1]
                    job.scan_reason = "Scan failed: " + why
                while job.scan_state == "done" and job.repair_queue:
                    fixed, original, status = job.repair_queue.pop(0)
                    try:
                        self._start_scan(job, url=fixed)
                        self.wake.set()
                        return                     # this scan's reader carries on from here
                    except OSError:
                        job.broken_seen.add(original)
                        job.broken_links.append({"url": original, "status": status})
                        job.broken = len(job.broken_links)
                check = job.scan_state == "done" and bool(job.broken_links)
                if check:
                    job.scan_state = "scanning"     # still busy: checking the broken links
                    links, gen = list(job.broken_links), job.scan_gen
                else:
                    self._log_scan_end(job, 0)
        if check:
            self._check_broken(job, links, gen)
        self.wake.set()

    def _log_scan_end(self, job, dropped):
        known = sum(f.size for f in job.files if f.size is not None)
        job.log.append("Scan %s: %d files, %s, %d broken links, %d names corrected and retried, "
                       "%d dead links inside pages left out, file types: %s" % (
                           {"stopped": "stopped", "failed": "failed"}.get(job.scan_state, "finished"),
                           len(job.files), fmt_size(known), job.broken, len(job.repair_tried), dropped,
                           ", ".join(job.types) or "all"))

    def _fetch_listing(self, job, url, gen):
        """url_keys linked from the folder listing at `url`: an empty set if the server has no
        such folder, None if it couldn't be checked (the link is then kept)."""
        with self.lock:
            if job.scan_gen != gen or job.scan_stop_requested:
                return None
            try:
                proc = popen([self.wget] + LISTING_ARGS + [url], stdout=subprocess.PIPE)
            except OSError:
                return None
            job.scan_proc = proc              # so Pause/Remove can stop it
        out, _ = proc.communicate()
        if proc.returncode == 8:              # the server answered with an error, e.g. 404
            return set()
        if proc.returncode != 0:
            return None
        return listing_keys(url, out.decode("utf-8", "replace"))

    def _check_broken(self, job, links, gen):
        """Keep the broken links that their folder's listing links to; leave out the rest,
        which came from links inside downloaded pages."""
        listings, keep, dropped = {}, [], []
        for link in links:
            listed = None
            for cand in filter(None, (link["url"], unmangle_url(link["url"]))):
                folder = parent_url(cand)
                if folder not in listings:
                    listings[folder] = (self._fetch_listing(job, folder, gen)
                                        if len(listings) < LISTING_LIMIT else None)
                keys = listings[folder]
                if keys is None:
                    listed = True             # couldn't check: keep it
                    break
                if url_key(cand) in keys:
                    listed = True
                    break
                listed = False
            (keep if listed else dropped).append(link)
        with self.lock:
            if job.scan_gen != gen:
                return                        # a new scan started meanwhile
            job.scan_state = "stopped" if job.scan_stop_requested else "done"
            job.broken_links = keep
            job.broken = len(keep)
            for link in dropped:
                job.log.append("Left out (not in its folder's listing): %s" % link["url"])
            self._log_scan_end(job, len(dropped))
        self.wake.set()

    def _add_file(self, job, url, size):
        if not url or urlparse(url).scheme not in ("http", "https", "ftp"):
            return
        f = job.by_url.get(url)
        if f:
            if f.size is None:
                f.size = size
            return
        f = FileItem(job, len(job.files), url, size)
        job.log.append("Found: %s (%s)" % (f.rel, fmt_size(size)))
        if job.check_local:
            self._classify(f)
            if job.kind == "download" and f.status in ("missing", "partial"):
                f.status = "paused" if job.paused else "queued"
        elif job.paused:
            f.status = "paused"
        job.files.append(f)
        job.by_url[url] = f
        self.wake.set()

    def _classify(self, f):
        """In the folder at the path a download would use, or anywhere under the same name
        and size: local. At that path but smaller: partial (--continue resumes it). Else missing."""
        job = f.job
        f.local = ""
        final = self._final_path(f)
        if os.path.isfile(final):
            have = os.path.getsize(final)
            if f.size is not None and have < f.size:
                f.status, f.done = "partial", have
            else:
                f.status, f.done = "local", have if f.size is None else f.size
            return
        part = self._part_path(f)
        if os.path.isfile(part) and os.path.getsize(part) > 0:     # an interrupted download
            f.status, f.done = "partial", os.path.getsize(part)
            return
        name = os.path.basename(final).lower()
        raw = self._url_name(f).lower()
        for path, have in job.index.get(name, []) + (job.index.get(raw, []) if raw != name else []):
            if f.size is not None and have == f.size:      # a name alone isn't enough
                f.status, f.done = "local", have
                f.local = os.path.relpath(path, job.target)
                return
        f.status, f.done = "missing", 0

    # ---- downloading ----

    def _loop(self):
        while self._running:
            self.wake.wait(0.5)
            self.wake.clear()
            with self.lock:
                self._measure()
                self._schedule()

    def _measure(self):
        now = time.monotonic()
        for f in self.active:
            path = f.dest
            if not path:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if f.mark is None:
                f.mark = (now, size)
            else:
                t0, b0 = f.mark
                if now - t0 >= 0.4:
                    inst = max(0.0, (size - b0) / (now - t0))
                    f.speed = inst if f.speed == 0 else 0.6 * f.speed + 0.4 * inst
                    f.mark = (now, size)
            f.done = size

    def _schedule(self):
        for job in self.jobs:
            if len(self.active) >= self.limit:
                return
            i = job.cursor
            while i < len(job.files) and len(self.active) < self.limit:
                if job.files[i].status == "queued":
                    self._start_file(job.files[i])
                i += 1
            while job.cursor < len(job.files) and job.files[job.cursor].status != "queued":
                job.cursor += 1

    def _start_file(self, f):
        job = f.job
        final, part = self._final_path(f), self._part_path(f)
        try:
            if os.path.isfile(final) and not os.path.exists(part):
                have = os.path.getsize(final)
                if f.size is None or have >= f.size:       # already here: nothing to fetch
                    f.status, f.done, f.error = "done", have, ""
                    if f.size is None:
                        f.size = have
                    return
            os.makedirs(os.path.dirname(part), exist_ok=True)
            if os.path.isfile(final) and not os.path.exists(part):
                os.replace(final, part)                    # incomplete copy: continue from it
            cmd = [self.wget] + FILE_ARGS + ["--output-document", os.path.basename(part), f.url]
            proc = popen(cmd, cwd=os.path.dirname(part))
        except OSError as e:
            f.status, f.error = "failed", str(e)
            job.log.append("Failed: %s: %s" % (f.rel, f.error))
            return
        f.proc = proc
        f.status = "active"
        f.pause_requested = False
        f.error = ""
        f.speed = 0.0
        f.mark = None
        f.dest = part
        self.active.append(f)
        threading.Thread(target=self._file_reader, args=(f, proc), daemon=True).start()

    @staticmethod
    def _url_name(f):
        p = urlparse(f.url)
        name = unquote(p.path).rstrip("/").rsplit("/", 1)[-1] or "index.html"
        return name + ("?" + unquote(p.query) if p.query else "")

    def _final_path(self, f):
        """Where a finished file goes: target + its folders relative to the start URL + its name."""
        return os.path.join(self._dest_dir(f), safe_name(self._url_name(f)))

    def _part_path(self, f):
        """Where wget writes it meanwhile. Named from the URL, so a resume finds it again."""
        key = hashlib.sha1(f.url.encode("utf-8")).hexdigest()[:20]
        return os.path.join(f.job.target, PARTIAL_DIR, key + ".part")

    def _dest_dir(self, f):
        """Folder a file is saved in: target + its folders relative to the start URL."""
        return os.path.join(f.job.target, *safe_dirs(f.rel))

    def _pause_active(self, f):
        f.pause_requested = True
        terminate(f.proc)

    def _file_reader(self, f, proc):
        error = ""
        tail = collections.deque(maxlen=4)
        for line in lines(proc.stderr):
            if not line.strip() or PROGRESS_LINE_RE.match(line):
                continue
            tail.append(line.strip())
            m = HTTP_ERROR_RE.search(line)
            if m:
                error = "HTTP %s %s" % (m.group(1), m.group(2))
        rc = proc.wait()
        with self.lock:
            if f in self.active:
                self.active.remove(f)
            f.proc = None
            f.speed = 0.0
            if f.pause_requested:
                f.status = "paused"
            elif rc == 0:
                final = self._final_path(f)
                try:
                    if f.dest and os.path.isfile(f.dest):
                        os.makedirs(os.path.dirname(final), exist_ok=True)
                        os.replace(f.dest, final)          # the app names it, not wget
                    f.status, f.done = "done", os.path.getsize(final)
                    if f.size is None:
                        f.size = f.done
                except OSError as e:
                    f.status = "failed"
                    f.error = "Downloaded, but couldn't save it as %s: %s" % (final, e.strerror or e)
                    f.job.log.append("Failed: %s: %s" % (f.rel, f.error))
            else:
                f.status = "failed"
                f.error = error or WGET_EXIT.get(rc, "wget exit %d" % rc)
                f.job.log.append("Failed: %s: %s" % (f.rel, f.error))
                for t in tail:
                    f.job.log.append("    " + t)
                try:
                    if f.dest and os.path.getsize(f.dest) == 0:
                        os.remove(f.dest)                  # wget creates the file even on a 404
                except OSError:
                    pass
            self._tidy_partials(f.job)
        self.wake.set()

    def _tidy_partials(self, job):
        """Remove the partial folder once it's empty and nothing is downloading into it.
        (Runs under the lock, and _start_file registers a download under the same lock before
        wget has created its file, so a starting download is never left without its folder.)"""
        if any(a.job.target == job.target for a in self.active):
            return
        try:
            os.rmdir(os.path.join(job.target, PARTIAL_DIR))   # only succeeds once it's empty
        except OSError:
            pass

    # ---- views ----

    def _summary(self, job):
        counts = collections.Counter(f.status for f in job.files)
        sizes = [f.size for f in job.files if f.size is not None]
        bytes_total = sum(sizes)
        bytes_done = sum(min(f.done, f.size) if f.size is not None else f.done for f in job.files)
        speed = sum(f.speed for f in self.active if f.job is job)
        scanning = job.scan_state == "scanning"
        if counts["active"]:
            status = "downloading"
        elif counts["queued"]:
            status = "waiting"
        elif scanning:
            status = "scanning"
        elif counts["paused"] or job.scan_state == "stopped":
            status = "paused"
        elif job.scan_state == "failed" and not job.files:
            status = "failed"
        else:
            status = "done"
        reason = job.scan_reason
        if not reason and status == "done" and not job.files:
            reason = ("No files matched the file types (%s). Clear the File types field to include every type."
                      % ", ".join(job.types)) if job.types else "No files found at this URL."
        notes = []
        if job.check_local and job.folder_exists is False:
            notes.append("This folder doesn't exist yet, so every file counts as missing.")
        if job.scan_state == "done" and job.scan_errors:
            more = len(job.scan_errors) - 1
            notes.append("The scan finished with errors, so a few folders may be missing from the list: %s%s"
                         % (job.scan_errors[0], " (and %d more in the log)" % more if more else ""))
        note = " ".join(notes)
        missing_bytes = sum(f.size - f.done for f in job.files
                            if f.status in ("missing", "partial") and f.size is not None)
        eta = None
        if speed > 0 and not scanning:
            eta = max(0, bytes_total - bytes_done) / speed
        return {
            "id": job.id, "kind": job.kind, "url": job.url, "target": job.target,
            "types": job.types, "status": status, "scan_state": job.scan_state,
            "reason": reason, "note": note, "broken": job.broken,
            "check_local": job.check_local, "missing_bytes": missing_bytes,
            "files_total": len(job.files), "counts": dict(counts),
            "bytes_total": bytes_total, "bytes_done": bytes_done,
            "unknown": len(job.files) - len(sizes), "speed": round(speed),
            "eta": None if eta is None else round(eta),
            # shown under the job when collapsed: what is moving, then what needs a click
            "rows": [f.view() for f in self._rows(job)],
        }

    def _rows(self, job):
        active = [f for f in self.active if f.job is job]
        failed = [f for f in job.files if f.status == "failed"][:5]
        paused = [f for f in job.files if f.status == "paused"][:5]
        return active + failed + paused

    def state(self):
        with self.lock:
            jobs = [self._summary(j) for j in reversed(self.jobs)]
            queued = sum(j["counts"].get("queued", 0) for j in jobs)
            return {
                "limit": self.limit, "limit_min": LIMIT_MIN, "limit_max": LIMIT_MAX,
                "default_target": self.default_target,
                "totals": {"active": len(self.active), "queued": queued,
                           "speed": round(sum(f.speed for f in self.active))},
                "jobs": jobs,
            }

    def files_page(self, job, status, offset, limit):
        with self.lock:
            if status == "broken":
                links = job.broken_links[offset:offset + limit]
                return {
                    "counts": dict(collections.Counter(f.status for f in job.files)),
                    "total": len(job.broken_links), "offset": offset, "files": [],
                    "links": [dict(l, path=job.relative(l["url"]) or l["url"]) for l in links],
                    "log": list(job.log)[-300:],
                }
            items = job.files if status == "all" else [f for f in job.files if f.status == status]
            return {
                "counts": dict(collections.Counter(f.status for f in job.files)),
                "total": len(items), "offset": offset,
                "files": [f.view() for f in items[offset:offset + limit]],
                "log": list(job.log)[-300:],
            }


def make_handler(app):
    page = PAGE.replace("__TOKEN__", app.token).encode("utf-8")
    job_re = re.compile(r"^/api/jobs/(\d+)/(pause|resume|rescan|retry|download|remove)$")
    file_re = re.compile(r"^/api/jobs/(\d+)/files/(\d+)/(pause|resume)$")
    files_re = re.compile(r"^/api/jobs/(\d+)/files$")
    statuses = {"all", "queued", "active", "paused", "done", "failed", "missing", "partial", "local", "broken"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, need_token=True):
            # Host check blocks DNS rebinding; the token header blocks cross-site requests.
            if self.headers.get("Host", "") not in app.allowed_hosts:
                self._send(403, {"error": "bad host"})
                return False
            if need_token and self.headers.get("X-Token") != app.token:
                self._send(403, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                if self._guard(need_token=False):
                    self._send(200, page, "text/html; charset=utf-8")
                return
            if not self._guard():
                return
            if url.path == "/api/state":
                return self._send(200, app.state())
            m = files_re.match(url.path)
            if m:
                job = app.get(int(m.group(1)))
                if not job:
                    return self._send(404, {"error": "no such job"})
                q = parse_qs(url.query)
                status = q.get("status", ["all"])[0]
                if status not in statuses:
                    return self._send(400, {"error": "bad status"})
                try:
                    offset = max(0, int(q.get("offset", ["0"])[0]))
                    limit = max(1, min(100000, int(q.get("limit", ["200"])[0])))
                except ValueError:
                    return self._send(400, {"error": "bad paging"})
                return self._send(200, app.files_page(job, status, offset, limit))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad request body"})
            path = urlparse(self.path).path

            if path == "/api/jobs":
                kind = body.get("kind", "download")
                if kind not in ("download", "compare"):
                    return self._send(400, {"error": "bad kind"})
                try:
                    job = app.add_job(kind, body.get("url"), body.get("target"), body.get("types"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"id": job.id, "url": job.url})

            if path == "/api/pick-folder":
                if not app.picker_lock.acquire(blocking=False):
                    return self._send(409, {"error": "A folder window is already open."})
                try:
                    chosen = pick_folder(str(body.get("initial") or ""))
                finally:
                    app.picker_lock.release()
                if chosen is None:
                    return self._send(501, {"error": "No folder window is available on this system. Type the path instead."})
                return self._send(200, {"path": chosen})   # "" = cancelled

            if path == "/api/settings":
                try:
                    limit = app.set_limit(body.get("limit"))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "limit must be a number"})
                return self._send(200, {"limit": limit})

            m = job_re.match(path)
            if m:
                job = app.get(int(m.group(1)))
                if not job:
                    return self._send(404, {"error": "no such job"})
                try:
                    app.job_action(job, m.group(2))
                except OSError as e:
                    return self._send(400, {"error": "Could not start wget: %s" % e})
                return self._send(200, {"ok": True})

            m = file_re.match(path)
            if m:
                job = app.get(int(m.group(1)))
                if not job or not app.file_action(job, int(m.group(2)), m.group(3)):
                    return self._send(404, {"error": "no such file"})
                return self._send(200, {"ok": True})

            self._send(404, {"error": "not found"})

    return Handler


def create_server(app, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    real_port = server.server_address[1]
    app.allowed_hosts = {"127.0.0.1:%d" % real_port, "localhost:%d" % real_port}
    return server


def find_wget():
    """A wget next to the program wins (so it can be swapped), then the copy bundled in the
    executable, then PATH."""
    if FROZEN:
        places = [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")]
    else:
        places = [os.path.dirname(os.path.abspath(__file__))]
    for here in places:
        for name in ("wget.exe", "wget"):
            local = os.path.join(here, name)
            if here and os.path.isfile(local) and os.access(local, os.X_OK):
                return local
    return shutil.which("wget")


def default_target():
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    base = downloads if os.path.isdir(downloads) else os.path.expanduser("~")
    return os.path.join(base, "wget")


def main():
    ap = argparse.ArgumentParser(description="Local web frontend for wget")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--check", action="store_true", help="print which wget will be used, then exit")
    ap.add_argument("--pick-folder", metavar="START", nargs="?", const="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.pick_folder is not None:
        run_tk_picker(args.pick_folder)
        return

    wget = find_wget()
    if args.check:
        if not wget:
            sys.exit("wget: not found")
        version = subprocess.run([wget, "--version"], capture_output=True, env=wget_env())
        print("wget: %s" % wget)
        print(version.stdout.decode("utf-8", "replace").splitlines()[0] if version.stdout else "wget did not run")
        sys.exit(0 if version.returncode == 0 else 1)
    if not wget:
        sys.exit("wget not found. Install it, or put wget.exe next to this program.")

    app = App(wget, default_target())
    try:
        server = create_server(app, args.port)
    except OSError:
        server = create_server(app, 0)   # port taken: pick any free one
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("wget frontend running at %s  (Ctrl+C to quit; running downloads are stopped)" % url)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>wget</title>
<style>
:root {
  --ground: #edf0f2; --surface: #ffffff; --ink: #16202a; --muted: #5e6b77; --line: #d3dae0;
  --track: #e2e7eb; --signal: #0b6e7f; --signal-ink: #ffffff;
  --done: #2e7d32; --paused: #9a6200; --failed: #b3261e;
  --stripe: rgba(255, 255, 255, .22);
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground: #14181c; --surface: #1b2026; --ink: #e6eaee; --muted: #95a1ac; --line: #2c333b;
    --track: #262d35; --signal: #3fb6c6; --signal-ink: #062a30;
    --done: #6fc276; --paused: #e2a93b; --failed: #f0857c;
    --stripe: rgba(0, 0, 0, .18);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--ground); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif; padding-bottom: 72px; }
main { max-width: 980px; margin: 0 auto; padding: 22px 16px 32px; }
h1 { font-size: 18px; font-weight: 650; margin: 0 0 14px; letter-spacing: -.01em; }
.num { font-variant-numeric: tabular-nums; }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* form */
form { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px;
  display: grid; grid-template-columns: minmax(0, 1fr) 120px auto; gap: 10px 12px; align-items: end; }
.field label { display: block; font-size: 12.5px; color: var(--muted); margin-bottom: 4px; }
.field.url { grid-column: 1 / -1; }
input[type=text], input[type=url] { width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--ground); color: var(--ink); font: inherit; }
input:focus-visible { outline: 2px solid var(--signal); outline-offset: -1px; }
.pick { display: flex; gap: 6px; }
.pick input { flex: 1; min-width: 0; }
.form-actions { display: flex; gap: 8px; }
.btn { padding: 7px 13px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface);
  cursor: pointer; white-space: nowrap; }
.btn:hover { border-color: var(--muted); }
.btn.primary { background: var(--signal); border-color: var(--signal); color: var(--signal-ink); font-weight: 600; }
.btn:disabled { opacity: .45; cursor: default; }
#msg { grid-column: 1 / -1; color: var(--failed); font-size: 13px; margin: 0; }
#msg:empty { display: none; }

/* jobs */
#jobs { margin-top: 18px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
#jobs:empty { display: none; }
.empty { margin-top: 18px; color: var(--muted); padding: 24px 2px; }
.job { padding: 14px 16px 12px; }
.job + .job { border-top: 1px solid var(--line); }
.job-head { display: flex; align-items: center; gap: 10px; min-width: 0; }
.toggle { flex: none; width: 26px; height: 26px; display: grid; place-items: center; border: 0; border-radius: 6px;
  background: transparent; cursor: pointer; color: var(--muted); }
.toggle:hover { background: var(--ground); color: var(--ink); }
.toggle svg { width: 14px; height: 14px; transition: transform .15s ease; }
.toggle[aria-expanded=true] svg { transform: rotate(90deg); }
.job-title { min-width: 0; flex: 1; display: flex; align-items: baseline; gap: 8px; }
.jname { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.jhost { color: var(--muted); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.jstate { flex: none; display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.s-downloading .dot, .s-scanning .dot, .s-waiting .dot { background: var(--signal); }
.s-done .dot { background: var(--done); }
.s-paused .dot { background: var(--paused); }
.s-failed .dot { background: var(--failed); }
.job-actions { flex: none; display: flex; gap: 6px; }

.bar { position: relative; height: 6px; border-radius: 3px; background: var(--track); overflow: hidden; }
.bar .fill { position: absolute; inset: 0 auto 0 0; width: 0; border-radius: inherit; background: var(--signal);
  transition: width .45s linear; }
.bar.big { height: 12px; border-radius: 6px; margin: 12px 0 8px 36px; }
.s-done .bar.big .fill { background: var(--done); }
.s-paused .bar.big .fill { background: var(--paused); }
.s-failed .bar.big .fill { background: var(--failed); }
.s-downloading .bar.big .fill {
  background-image: repeating-linear-gradient(-45deg, var(--stripe) 0 7px, transparent 7px 14px);
  background-size: 19.8px 19.8px; animation: crawl .8s linear infinite; }
@keyframes crawl { to { background-position: 19.8px 0; } }
.bar.indet .fill { width: 28% !important; transition: none; animation: sweep 1.3s ease-in-out infinite alternate; }
@keyframes sweep { from { left: 0; } to { left: 72%; } }

.jline { display: flex; align-items: baseline; gap: 18px; margin-left: 36px; flex-wrap: wrap; }
.stats { display: flex; gap: 18px; flex-wrap: wrap; flex: 1; color: var(--muted); }
.stats b { color: var(--ink); font-weight: 600; }
.pct { font-weight: 650; font-size: 15px; }
.where, .reason, .note { margin: 4px 0 0 36px; font-size: 12.5px; color: var(--muted); overflow-wrap: anywhere; }
.reason { color: var(--failed); }
.reason:empty, .note:empty { display: none; }

/* file rows */
.files { list-style: none; margin: 10px 0 0 36px; padding: 0; }
.files:empty { display: none; }
.file { display: grid; grid-template-columns: minmax(0, 1fr) minmax(70px, 150px) 3.2em 9.6em 8.4em 28px;
  grid-template-areas: "name bar pct size state btn"; gap: 12px; align-items: center;
  padding: 6px 0; border-top: 1px solid var(--line); font-size: 13px; }
.files .file:first-child { border-top: 0; }
.fname { grid-area: name; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fname .dir { color: var(--muted); }
.file .bar { grid-area: bar; }
.fpct { grid-area: pct; text-align: right; }
.fsize { grid-area: size; text-align: right; color: var(--muted); }
.fstate { grid-area: state; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file .icon { grid-area: btn; }
.file.s-done .bar .fill { background: var(--done); }
.file.s-paused .bar .fill { background: var(--paused); }
.file.s-failed .bar .fill { background: var(--failed); }
.file.s-failed .fstate { color: var(--failed); }
.file.s-done .fstate, .file.s-local .fstate { color: var(--done); }
.file.s-local .bar .fill { background: var(--done); }
.file.s-partial .bar .fill { background: var(--paused); }
.file.s-missing .fstate { color: var(--ink); }
.icon { width: 28px; height: 28px; display: grid; place-items: center; border: 1px solid transparent; border-radius: 6px;
  background: transparent; cursor: pointer; color: var(--muted); padding: 0; }
.icon:hover { border-color: var(--line); color: var(--ink); }
.icon svg { width: 14px; height: 14px; fill: currentColor; }
.icon[hidden] { display: none; }

/* expanded panel */
.panel { margin: 12px 0 0 36px; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--line); }
.tab { border: 0; background: none; padding: 6px 10px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent;
  margin-bottom: -1px; font-size: 13px; }
.tab[aria-selected=true] { color: var(--ink); border-bottom-color: var(--signal); font-weight: 600; }
.panel .files { margin-left: 0; margin-top: 4px; }
.panel-empty { color: var(--muted); margin: 12px 0 4px; }
.file.link-row { grid-template-columns: minmax(0, 1fr) minmax(8em, auto); grid-template-areas: "name state"; }
a.fname { color: var(--ink); text-decoration: none; }
a.fname:hover .base { text-decoration: underline; }
.file.link-row .fstate { text-align: right; color: var(--failed); }
.panel-tools { display: flex; justify-content: flex-end; margin-top: 8px; }
.panel-tools[hidden] { display: none; }
.linkish { border: 0; background: none; padding: 0; color: inherit; cursor: pointer; text-decoration: underline;
  text-decoration-color: var(--line); text-underline-offset: 3px; }
.linkish:hover { color: var(--ink); text-decoration-color: currentColor; }
.panel-empty:empty { display: none; }
.more { margin-top: 8px; }
.log { margin-top: 10px; }
.log summary { cursor: pointer; color: var(--muted); font-size: 12.5px; }
.log pre { margin: 6px 0 0; max-height: 260px; overflow: auto; background: var(--ground); border: 1px solid var(--line);
  border-radius: 6px; padding: 8px; font: 12px/1.45 ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-all; }

/* footer */
.dock { position: fixed; inset: auto 0 0 0; background: var(--surface); border-top: 1px solid var(--line); }
.dock-inner { max-width: 980px; margin: 0 auto; padding: 10px 16px; display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.stepper { display: flex; align-items: center; gap: 10px; }
.stepper label { color: var(--muted); }
.stepper-box { display: inline-flex; align-items: stretch; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; }
.stepper-box button { width: 32px; border: 0; background: var(--ground); cursor: pointer; font-size: 16px; line-height: 1; }
.stepper-box button:hover:not(:disabled) { background: var(--track); }
.stepper-box button:disabled { opacity: .35; cursor: default; }
.stepper-box input { width: 42px; border: 0; border-left: 1px solid var(--line); border-right: 1px solid var(--line);
  text-align: center; font: inherit; font-weight: 600; background: var(--surface); color: var(--ink); padding: 5px 0;
  -moz-appearance: textfield; }
.stepper-box input::-webkit-inner-spin-button, .stepper-box input::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
.stepper-box input:focus-visible { outline: 2px solid var(--signal); outline-offset: -2px; }
#totals { color: var(--muted); flex: 1; }
#speed { font-weight: 650; }

@media (max-width: 680px) {
  form { grid-template-columns: minmax(0, 1fr) 96px; }
  .field.folder, .form-actions { grid-column: 1 / -1; }
  body { padding-bottom: 100px; }
  .job-head { flex-wrap: wrap; row-gap: 6px; }
  .job-title { flex: 1 1 calc(100% - 40px); flex-direction: column; gap: 0; }
  .jstate { margin-left: 36px; }
  .job-actions { margin-left: auto; }
  .dock-inner { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; padding: 8px 16px; }
  #totals { grid-column: 1 / -1; grid-row: 2; font-size: 12.5px; }
  .bar.big, .jline, .where, .reason, .files, .panel { margin-left: 0; }
  .file { grid-template-columns: minmax(0, 1fr) 3.2em 28px;
    grid-template-areas: "name name btn" "bar pct btn" "size state state"; gap: 4px 10px; }
  .file.link-row { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "name state"; }
  .fsize { text-align: left; }
  .fstate { text-align: right; }
}
@media (prefers-reduced-motion: reduce) {
  .bar .fill, .toggle svg { transition: none; }
  .s-downloading .bar.big .fill, .bar.indet .fill { animation: none; }
}
</style>
</head>
<body>
<main>
  <h1>wget</h1>
  <form id="form" autocomplete="off" novalidate>
    <div class="field url"><label for="url">URL</label>
      <input id="url" type="url" placeholder="https://example.com/files/" spellcheck="false"></div>
    <div class="field folder"><label for="target">Folder</label>
      <div class="pick"><input id="target" type="text" spellcheck="false">
        <button class="btn" type="button" id="browse" title="Opens your system's folder window">Browse…</button></div></div>
    <div class="field"><label for="types">File types</label><input id="types" type="text" placeholder="pdf, epub… (empty = all)" spellcheck="false"></div>
    <div class="form-actions">
      <button class="btn primary" type="submit" data-kind="download">Download</button>
      <button class="btn" type="submit" data-kind="compare">Compare with folder</button>
    </div>
    <p id="msg" role="alert"></p>
  </form>
  <p class="empty" id="empty">Paste a folder URL above. Download fetches every matching file into the folder; Compare with folder lists the files you don't have yet and how big they are.</p>
  <section id="jobs" aria-label="Downloads"></section>
</main>
<footer class="dock">
  <div class="dock-inner">
    <div class="stepper" role="group" aria-labelledby="par-label">
      <label id="par-label" for="par">Parallel downloads</label>
      <span class="stepper-box">
        <button type="button" id="dec" aria-label="Fewer parallel downloads">&minus;</button>
        <input id="par" type="number" inputmode="numeric" min="1" max="10" step="1" value="3"
          title="Applies immediately. When lowered, running files finish before new ones start.">
        <button type="button" id="inc" aria-label="More parallel downloads">+</button>
      </span>
    </div>
    <span id="totals" class="num" aria-live="polite"></span>
    <span id="speed" class="num"></span>
  </div>
</footer>

<template id="job-tpl">
  <article class="job">
    <div class="job-head">
      <button class="toggle" type="button" aria-expanded="false">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span class="sr">Show all files</span>
      </button>
      <div class="job-title"><span class="jname"></span><span class="jhost"></span></div>
      <span class="jstate"><span class="dot"></span><span class="jstate-text"></span></span>
      <div class="job-actions"></div>
    </div>
    <div class="bar big" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="fill"></div></div>
    <div class="jline"><span class="stats num"></span><span class="pct num"></span></div>
    <p class="where"></p>
    <p class="note"></p>
    <p class="reason"></p>
    <ul class="files active-list" aria-label="Files in progress"></ul>
    <section class="panel" hidden>
      <div class="tabs" role="tablist" aria-label="Filter files"></div>
      <div class="panel-tools" hidden><button class="btn copy-broken" type="button">Copy all URLs</button></div>
      <ul class="files all-list"></ul>
      <ul class="files broken-list" hidden></ul>
      <p class="panel-empty"></p>
      <button class="btn more" type="button" hidden>Show more</button>
      <details class="log"><summary>Log</summary><pre></pre></details>
    </section>
  </article>
</template>

<script>
"use strict";
const TOKEN = "__TOKEN__";
const $ = (s, el = document) => el.querySelector(s);
const ICON = {
  pause: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3" width="3" height="10" rx="1"/><rect x="9" y="3" width="3" height="10" rx="1"/></svg>',
  resume: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6c0 .5.5.8.9.5l7.2-4.8c.4-.3.4-.8 0-1.1L5.9 2.7c-.4-.3-.9 0-.9.5z"/></svg>',
  retry: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5a5.5 5.5 0 1 0 5.4 6.5h-1.6A4 4 0 1 1 8 4c1 0 2 .4 2.7 1.1L8.8 7H14V1.8l-2.1 2.1A5.5 5.5 0 0 0 8 2.5z"/></svg>',
  download: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7.2 2h1.6v6.6l2.4-2.4 1.1 1.1L8 11.6 3.7 7.3l1.1-1.1 2.4 2.4z"/><rect x="3" y="12.6" width="10" height="1.6" rx=".8"/></svg>',
};
const FILE_STATE = { active: "Downloading", queued: "Waiting", paused: "Paused", done: "Done", failed: "Failed",
  missing: "Missing", partial: "Incomplete", local: "In folder" };
const JOB_STATE = { downloading: "Downloading", waiting: "Waiting", scanning: "Finding files", paused: "Paused", done: "Done", failed: "Failed" };
const TABS = [["all", "All"], ["missing", "Missing"], ["partial", "Incomplete"], ["active", "Downloading"], ["queued", "Waiting"],
  ["paused", "Paused"], ["done", "Done"], ["local", "In folder"], ["failed", "Failed"], ["broken", "Broken links"]];
const PAGE_SIZE = 200;

const jobEls = new Map();
const view = new Map();          // job id -> {open, filter, limit}
let limitMax = 10, limitMin = 1, defaultsSet = false, limitPending = 0, limitTimer = 0, busy = false;

function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

async function api(path, body) {
  const opts = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function size(n) {
  if (n == null) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 2 : n < 100 ? 1 : 0)) + " " + u[i];
}
function duration(s) {
  if (s == null) return "";
  if (s < 60) return "under a minute left";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min left`;
  return `${Math.floor(m / 60)} h ${m % 60} min left`;
}
function plural(n, word) { return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

function syncList(box, items, key, create, update) {
  const old = new Map([...box.children].map(el => [el.dataset.key, el]));
  items.forEach((it, i) => {
    const k = String(key(it));
    let el = old.get(k);
    if (el) old.delete(k); else { el = create(it); el.dataset.key = k; }
    update(el, it);
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
  });
  old.forEach(el => el.remove());
}

function setBar(bar, pct, indet) {
  bar.classList.toggle("indet", !!indet);
  const fill = bar.firstElementChild;
  fill.style.width = (pct == null ? 0 : Math.min(100, pct)) + "%";
  if (pct == null || indet) bar.removeAttribute("aria-valuenow"); else bar.setAttribute("aria-valuenow", Math.floor(pct));
}

async function act(path) {
  $("#msg").textContent = "";
  try { await api(path, {}); } catch (e) { $("#msg").textContent = e.message; }
  refresh();
}

/* ---- file rows ---- */

function fileRow(jobId) {
  return () => {
    const li = document.createElement("li");
    li.className = "file";
    li.innerHTML = '<span class="fname"><span class="dir"></span><span class="base"></span></span>' +
      '<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="fill"></div></div>' +
      '<span class="fpct num"></span><span class="fsize num"></span><span class="fstate"></span>' +
      '<button class="icon" type="button"></button>';
    const btn = $(".icon", li);
    btn.addEventListener("click", () => {
      btn.disabled = true;
      act(`/api/jobs/${jobId}/files/${li.dataset.fid}/${btn.dataset.act}`).finally(() => { btn.disabled = false; });
    });
    return li;
  };
}

function updateFile(li, f) {
  li.dataset.fid = f.id;
  li.className = "file s-" + f.status;
  $(".dir", li).textContent = f.dir;
  $(".base", li).textContent = f.name;
  $(".fname", li).title = f.path;
  const complete = f.status === "done" || f.status === "local";
  const pct = f.size ? f.done / f.size * 100 : (complete ? 100 : null);
  const bar = $(".bar", li);
  setBar(bar, pct, f.status === "active" && !f.size);
  bar.setAttribute("aria-label", f.name);
  $(".fpct", li).textContent = pct == null ? "" : Math.floor(pct) + "%";
  $(".fsize", li).textContent =
    complete || f.status === "missing" || f.status === "queued" && !f.done ? size(f.size ?? f.done)
    : f.size ? `${size(f.done)} of ${size(f.size)}` : size(f.done);
  const st = $(".fstate", li);
  st.textContent = f.status === "active" ? (f.speed ? size(f.speed) + "/s" : "Starting")
    : f.status === "failed" ? f.error || "Failed"
    : f.status === "local" && f.local ? "Matched by name" : FILE_STATE[f.status];
  st.title = f.status === "failed" ? f.error : f.local ? `Found at ${f.local}` : "";
  const btn = $(".icon", li);
  const a = f.status === "active" || f.status === "queued" ? ["pause", "Pause", "pause"]
    : f.status === "paused" ? ["resume", "Resume", "resume"]
    : f.status === "failed" ? ["resume", "Retry", "retry"]
    : f.status === "missing" || f.status === "partial" ? ["resume", "Download", "download"] : null;
  btn.hidden = !a;
  if (a && btn.dataset.act + btn.dataset.label !== a[0] + a[1]) {
    btn.dataset.act = a[0];
    btn.dataset.label = a[1];
    btn.innerHTML = ICON[a[2]];
  }
  if (a) { btn.setAttribute("aria-label", `${a[1]} ${f.name}`); btn.title = a[1]; }
}

function brokenRow() {
  const li = document.createElement("li");
  li.className = "file link-row";
  li.innerHTML = '<a class="fname" target="_blank" rel="noopener noreferrer"><span class="dir"></span><span class="base"></span></a>' +
    '<span class="fstate"></span>';
  return li;
}

function updateBroken(li, l) {
  const a = $(".fname", li);
  a.href = l.url;
  a.title = l.url;
  const m = l.path.match(/^(.*\/)?([^/]+\/?)$/) || [null, "", l.path];
  $(".dir", li).textContent = m[1] || "";
  $(".base", li).textContent = m[2];
  $(".fstate", li).textContent = l.status || "No response";
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return; } catch (e) {}
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
}

/* ---- job rows ---- */

function jobActions(j) {
  const c = j.counts, acts = [];
  const missing = (c.missing || 0) + (c.partial || 0);
  const live = j.status === "downloading" || j.status === "waiting" || j.status === "scanning";
  const compare = j.kind === "compare";
  if (live) acts.push(["pause", compare && j.status === "scanning" ? "Stop" : "Pause"]);
  else if (j.status === "paused") acts.push(["resume", "Resume", !compare]);
  if (compare && missing) acts.push(["download", `Download ${plural(missing, "file")}`, true]);
  if (!live && j.status !== "paused")
    acts.push(["rescan", j.check_local ? "Compare again" : j.status === "failed" ? "Try again" : "Check for new files"]);
  if (c.failed) acts.push(["retry", "Retry failed"]);
  acts.push(["remove", "Remove"]);
  return acts;
}

function jobEl(j) {
  let el = jobEls.get(j.id);
  if (el) return el;
  el = $("#job-tpl").content.firstElementChild.cloneNode(true);
  el.dataset.key = j.id;
  const compare = j.kind === "compare";
  const st = { open: compare, filter: compare ? "missing" : "all", limit: PAGE_SIZE, kind: j.kind };
  view.set(j.id, st);
  const toggle = $(".toggle", el);
  const showPanel = () => {
    toggle.setAttribute("aria-expanded", st.open);
    $(".sr", toggle).textContent = st.open ? "Hide file list" : "Show all files";
    $(".panel", el).hidden = !st.open;
  };
  showPanel();
  $(".stats", el).addEventListener("click", ev => {
    const b = ev.target.closest("[data-open]");
    if (!b) return;
    st.open = true; st.filter = b.dataset.open; st.limit = PAGE_SIZE;
    showPanel();
    refresh();
  });
  $(".copy-broken", el).addEventListener("click", async ev => {
    const b = ev.currentTarget;
    b.disabled = true;
    try {
      const page = await api(`/api/jobs/${j.id}/files?status=broken&limit=100000`);
      await copyText(page.links.map(l => l.url).join("\n") + "\n");
      b.textContent = `Copied ${plural(page.links.length, "URL")}`;
    } catch (e) { b.textContent = "Couldn't copy"; }
    setTimeout(() => { b.textContent = "Copy all URLs"; b.disabled = false; }, 2000);
  });
  toggle.addEventListener("click", () => {
    st.open = !st.open;
    showPanel();
    refresh();
  });
  $(".more", el).addEventListener("click", () => { st.limit += PAGE_SIZE; refresh(); });
  $(".log", el).addEventListener("toggle", () => refresh());
  jobEls.set(j.id, el);
  return el;
}

function updateJob(el, j) {
  el.className = "job s-" + j.status;
  const u = new URL(j.url);
  $(".jname", el).textContent = decodeURIComponent(u.pathname) || "/";
  $(".jhost", el).textContent = u.host;
  $(".job-title", el).title = j.url;
  $(".jstate-text", el).textContent = JOB_STATE[j.status] || j.status;

  const c = j.counts;
  const compare = j.kind === "compare";
  const scanning = j.scan_state === "scanning";
  const finished = (c.done || 0) + (c.local || 0);
  const pct = j.bytes_total > 0 ? j.bytes_done / j.bytes_total * 100
    : j.files_total ? finished / j.files_total * 100 : null;
  const bar = $(".bar.big", el);
  setBar(bar, pct, scanning && !j.files_total);
  bar.setAttribute("aria-label", compare ? "Share already in the folder" : "Overall progress");
  $(".pct", el).textContent = pct == null ? "" : Math.floor(pct) + "%" + (compare ? " in folder" : "");

  const more = scanning ? " so far" : "";
  const s = [];
  if (compare) {
    s.push(`<span><b>${plural(j.files_total, "file")}</b> online${more}, ${size(j.bytes_total)}${j.unknown ? " + unknown" : ""}</span>`);
    s.push(`<span><b>${(c.local || 0).toLocaleString()}</b> in folder</span>`);
    s.push(`<span><b>${(c.missing || 0).toLocaleString()} missing</b></span>`);
    if (c.partial) s.push(`<span>${plural(c.partial, "incomplete file")}</span>`);
    if (j.missing_bytes) s.push(`<span>${size(j.missing_bytes)} to download</span>`);
    if (j.speed) s.push(`<span><b>${size(j.speed)}/s</b></span>`);
  } else {
    s.push(`<span><b>${finished.toLocaleString()}</b> of ${plural(j.files_total, "file")}${more}</span>`);
    s.push(`<span><b>${size(j.bytes_done)}</b> of ${size(j.bytes_total)}${j.unknown ? " + unknown" : ""}</span>`);
    if (j.speed) s.push(`<span><b>${size(j.speed)}/s</b></span>`);
    if (j.eta != null && j.status === "downloading") s.push(`<span>${duration(j.eta)}</span>`);
    if (c.local) s.push(`<span>${plural(c.local, "file")} already in folder</span>`);
  }
  if (c.failed) s.push(`<span>${plural(c.failed, "file")} failed</span>`);
  if (j.broken) s.push(`<span><button type="button" class="linkish" data-open="broken">${plural(j.broken, "broken link")}</button></span>`);
  $(".stats", el).innerHTML = s.join("");

  const types = j.types.length ? j.types.map(t => t.toUpperCase()).join(", ") + " files only" : "all file types";
  $(".where", el).textContent = compare ? `Compared with ${j.target}, ${types}` : `Saves to ${j.target}, ${types}`;
  $(".note", el).textContent = j.note || "";
  $(".reason", el).textContent = j.reason || "";

  // Rebuild the buttons only when the set changes; labels (counts) update in place,
  // so a button is never swapped out from under a click.
  const acts = jobActions(j);
  const box = $(".job-actions", el);
  const sig = acts.map(a => a[0] + (a[2] ? "!" : "")).join(";");
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = "";
    for (const [a, , primary] of acts) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn" + (primary ? " primary" : "");
      b.addEventListener("click", () => { b.disabled = true; act(`/api/jobs/${j.id}/${a}`); });
      box.appendChild(b);
    }
  }
  acts.forEach(([, label], i) => { if (box.children[i].textContent !== label) box.children[i].textContent = label; });

  const st = view.get(j.id);
  if (st.kind !== j.kind) {            // after "Download missing" the Missing list empties out
    if (st.filter === "missing" || st.filter === "partial") st.filter = "all";
    st.kind = j.kind;
  }
  const activeList = $(".active-list", el);
  activeList.hidden = st.open;
  if (!st.open) syncList(activeList, j.rows, f => f.id, fileRow(j.id), updateFile);
}

function updatePanel(el, j, page) {
  const st = view.get(j.id);
  const counts = page.counts, total = j.files_total;
  const tabs = $(".tabs", el);
  const shown = TABS.filter(([k]) => k === "all" || counts[k] || st.filter === k || (k === "broken" && j.broken));
  syncList(tabs, shown, t => t[0], ([k]) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "tab"; b.setAttribute("role", "tab");
    b.addEventListener("click", () => { st.filter = k; st.limit = PAGE_SIZE; refresh(); });
    return b;
  }, (b, [k, label]) => {
    b.textContent = `${label} ${(k === "all" ? total : k === "broken" ? j.broken : counts[k] || 0).toLocaleString()}`;
    b.setAttribute("aria-selected", st.filter === k);
  });
  const broken = st.filter === "broken";
  $(".all-list", el).hidden = broken;
  $(".broken-list", el).hidden = !broken;
  $(".panel-tools", el).hidden = !broken || !page.total;
  if (broken) syncList($(".broken-list", el), page.links, l => l.url, brokenRow, updateBroken);
  else syncList($(".all-list", el), page.files, f => f.id, fileRow(j.id), updateFile);
  const scanning = j.scan_state === "scanning";
  $(".panel-empty", el).textContent = page.total ? ""
    : broken ? "No broken links."
    : st.filter === "missing" ? (scanning ? "No missing files found so far." : "Nothing is missing. Every file is already in the folder.")
    : scanning && !total ? "Looking for files…" : "No files in this view.";
  const more = $(".more", el);
  const rest = page.total - (broken ? page.links : page.files).length;
  more.hidden = rest <= 0;
  more.textContent = `Show ${Math.min(rest, PAGE_SIZE).toLocaleString()} more of ${rest.toLocaleString()}`;
  const det = $(".log", el);
  if (det.open) {
    const pre = $("pre", det);
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
    pre.textContent = page.log.join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
}

/* ---- parallel downloads stepper ---- */

const par = $("#par");
function showLimit(v) {
  par.value = v;
  par.min = limitMin; par.max = limitMax;
  $("#dec").disabled = v <= limitMin;
  $("#inc").disabled = v >= limitMax;
}
function setLimit(v) {
  v = Math.max(limitMin, Math.min(limitMax, Math.round(Number(v)) || limitMin));
  showLimit(v);
  store("limit", v);
  limitPending++;
  clearTimeout(limitTimer);
  limitTimer = setTimeout(() => {
    api("/api/settings", { limit: v }).catch(e => { $("#msg").textContent = e.message; })
      .finally(() => { limitPending = 0; refresh(); });
  }, 250);
}
$("#dec").addEventListener("click", () => setLimit(Number(par.value) - 1));
$("#inc").addEventListener("click", () => setLimit(Number(par.value) + 1));
par.addEventListener("change", () => setLimit(par.value));
par.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); setLimit(par.value); } });

/* ---- refresh loop ---- */

async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const data = await api("/api/state");
    limitMax = data.limit_max; limitMin = data.limit_min;
    if (!defaultsSet) {
      defaultsSet = true;
      $("#target").value = load("target") || data.default_target;
      const t = load("types");
      $("#types").value = t === null ? "pdf" : t;
      const saved = Number(load("limit"));
      if (saved && saved !== data.limit) setLimit(saved); else showLimit(data.limit);
    } else if (!limitPending && document.activeElement !== par) {
      showLimit(data.limit);
    }

    const pages = await Promise.all(data.jobs.map(j => {
      const st = view.get(j.id);
      if (!st || !st.open) return null;
      return api(`/api/jobs/${j.id}/files?status=${st.filter}&limit=${st.limit}`).catch(() => null);
    }));

    syncList($("#jobs"), data.jobs, j => j.id, jobEl, updateJob);
    data.jobs.forEach((j, i) => { if (pages[i]) updatePanel(jobEls.get(j.id), j, pages[i]); });
    for (const id of [...jobEls.keys()]) if (!data.jobs.some(j => j.id === id)) { jobEls.delete(id); view.delete(id); }
    $("#empty").hidden = data.jobs.length > 0;

    const t = data.totals;
    const parts = [];
    parts.push(t.active > data.limit ? `${t.active} downloading, finishing down to ${data.limit}` : `${t.active} downloading`);
    if (t.queued) parts.push(`${t.queued.toLocaleString()} waiting`);
    $("#totals").textContent = parts.join(", ");
    $("#speed").textContent = t.speed ? size(t.speed) + "/s" : "";
  } catch (e) {
    $("#totals").textContent = "Lost connection to the app. Is it still running?";
  } finally {
    busy = false;
  }
}

$("#browse").addEventListener("click", async () => {
  const b = $("#browse");
  b.disabled = true;
  b.textContent = "Window open…";
  $("#msg").textContent = "";
  try {
    const r = await api("/api/pick-folder", { initial: $("#target").value });
    if (r.path) { $("#target").value = r.path; store("target", r.path); }
  } catch (e) { $("#msg").textContent = e.message; }
  finally { b.disabled = false; b.textContent = "Browse…"; $("#target").focus(); }
});

$("#form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const kind = ev.submitter ? ev.submitter.dataset.kind : "download";
  const body = { kind, url: $("#url").value, target: $("#target").value, types: $("#types").value };
  $("#msg").textContent = "";
  try {
    await api("/api/jobs", body);
    store("target", body.target);
    store("types", body.types);
    $("#url").value = "";
  } catch (e) { $("#msg").textContent = e.message; }
  refresh();
});

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
