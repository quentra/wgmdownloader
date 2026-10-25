"""End-to-end tests: real wget against a local fixture server, driven through the app's HTTP API.

Run:  python -m unittest -v test_wget_gui
"""

import glob
import hashlib
import http.server
import stat
import sys
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import wget_gui

# Local servers must not go through any configured proxy.
for _k in ("no_proxy", "NO_PROXY"):
    os.environ[_k] = "127.0.0.1,localhost"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BIG = 4 * 1024 * 1024


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static server with Range support, optional throttling, and concurrency tracking.
    Speaks HTTP/1.1 with keep-alive like nginx/Apache; the stdlib default (HTTP/1.0) makes
    wget's spider retry on closed connections, which slows scans and skews timing."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def _serve(self, head):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().do_HEAD() if head else super().do_GET()
        if not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start = 0
        if not head:
            self.server.gets.append(self.path)
        rng = self.headers.get("Range")
        if rng:
            self.server.ranges.append((self.path, rng))
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(size - start))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if head:
            return
        srv = self.server
        with srv.count_lock:
            srv.in_flight += 1
            srv.max_in_flight = max(srv.max_in_flight, srv.in_flight)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    if srv.throttle:          # sleep before writing, so the count drops as soon as the last byte is sent
                        time.sleep(srv.throttle)
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
        finally:
            with srv.count_lock:
                srv.in_flight -= 1


def wait_for(pred, timeout=30, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out waiting")


class WgetGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wget-gui-test-")
        cls.root = root = os.path.join(cls.tmp, "site")
        files = {
            "docs/a.pdf": b"a" * 1000,
            "docs/notes.txt": b"n" * 50,
            "docs/sub/c.PDF": b"c" * 2000,
            "docs/sub/deeper/d.pdf": b"d" * 3000,
            "docs/sp ace/ü file.pdf": b"u" * 400,
            "other/outside.pdf": b"x" * 10,   # parent of the crawl root; must be skipped
            "broken/index.html": b'<a href="ok.pdf">ok</a> <a href="gone.pdf">gone</a> '
                                 b'<a href="sub/lost%20file.pdf">lost</a> <a href="nodir/">nodir</a>',
            "broken/ok.pdf": b"o" * 500,
            # A web page saved into an archive: its images and one of its links point at files
            # that were never uploaded (the folder was renamed with underscores). Those are not
            # broken links of the archive; only what a folder listing links to counts.
            "pages/Saved Page.htm": b'<img src="Saved%20Page_files/logo.gif"> '
                                    b'<link rel="stylesheet" href="Saved%20Page_files/style.css"> '
                                    b'<a href="old_name.pdf">old</a> <a href="real%20name.pdf">real</a>',
            "pages/real name.pdf": b"r" * 600,
            "pages/Saved_Page_files/logo.gif": b"l" * 60,
            "flaky/x.pdf": b"f" * 700,
            "intl/Za\u017c\u00f3\u0142\u0107/g\u0119\u015bl\u0105 ja\u017a\u0144.pdf": b"p" * 321,
            "intl/\u00fc \u041a\u0438\u0457\u0432.pdf": b"k" * 222,
        }
        # Links written with raw (unescaped) UTF-8 characters, as many real servers do.
        files["rawlinks/index.html"] = ('<a href="Genevi\u00e8ve.pdf">1</a> <a href="It\u2019s.pdf">2</a> '
                                        '<a href="\u6587\u4ef6/">3</a>').encode("utf-8")
        files["rawlinks/Genevi\u00e8ve.pdf"] = b"g" * 111
        files["rawlinks/It\u2019s.pdf"] = b"i" * 222
        files["rawlinks/\u6587\u4ef6/inner.pdf"] = b"n" * 333
        # Links already garbled the way Windows wget garbles them (UTF-8 re-read as
        # Windows-1252): the server answers 404, and the app must find the real files.
        files["mangled/index.html"] = (b'<a href="Genevi%C3%83%C2%A8ve.pdf">1</a> '
                                       b'<a href="It%C3%A2%E2%82%AC%E2%84%A2s.pdf">2</a> '
                                       b'<a href="%C3%A6%E2%80%93%E2%80%A1%C3%A4%C2%BB%C2%B6/">3</a> '
                                       b'<a href="plain.pdf">4</a> <a href="gon%C3%83%C2%A8.pdf">5</a>')
        files["mangled/Genevi\u00e8ve.pdf"] = b"g" * 111
        files["mangled/It\u2019s.pdf"] = b"i" * 222
        files["mangled/\u6587\u4ef6/inner.pdf"] = b"n" * 333
        files["mangled/\u6587\u4ef6/Za\u017c\u00f3\u0142\u0107.pdf"] = b"z" * 555
        files["mangled/plain.pdf"] = b"p" * 444
        for i in range(6):
            files["many/f%d.pdf" % i] = os.urandom(512 * 1024)
        for i in range(4):
            files["slowjob/s%d.pdf" % i] = os.urandom(1024 * 1024)
        for rel, data in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        cls.big_src = os.path.join(root, "slow", "big.pdf")
        os.makedirs(os.path.dirname(cls.big_src))
        with open(cls.big_src, "wb") as f:
            f.write(os.urandom(BIG))

        handler = lambda *a, **kw: RangeHandler(*a, directory=root, **kw)
        cls.fixture = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.fixture.throttle = 0
        cls.fixture.ranges = []
        cls.fixture.gets = []
        cls.fixture.count_lock = threading.Lock()
        cls.fixture.in_flight = 0
        cls.fixture.max_in_flight = 0
        cls.fixture.daemon_threads = True
        threading.Thread(target=cls.fixture.serve_forever, daemon=True).start()
        cls.site = "http://127.0.0.1:%d" % cls.fixture.server_address[1]

        wget = wget_gui.find_wget()
        if not wget:
            raise unittest.SkipTest("wget not installed")
        cls.app = wget_gui.App(wget, os.path.join(cls.tmp, "default-target"))
        cls.server = wget_gui.create_server(cls.app, 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.server.shutdown()
        cls.fixture.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.fixture.throttle = 0
        self.call("/api/settings", {"limit": 3})

    # ---- helpers ----

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET")
        if token:
            req.add_header("X-Token", self.app.token)
        if host:
            req.add_header("Host", host)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with _NO_PROXY.open(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def new_job(self, kind, path, target=None, types="pdf"):
        body = {"kind": kind, "url": self.site + path, "types": types}
        if target:
            body["target"] = target
        code, data = self.call("/api/jobs", body)
        self.assertEqual(code, 200, data)
        return data["id"]

    def job(self, job_id):
        _, data = self.call("/api/state")
        return next(j for j in data["jobs"] if j["id"] == job_id)

    def files(self, job_id, status="all"):
        _, data = self.call("/api/jobs/%d/files?status=%s&limit=2000" % (job_id, status))
        return data

    def wait_status(self, job_id, *statuses, timeout=30):
        return wait_for(lambda: (lambda j: j if j["status"] in statuses else None)(self.job(job_id)),
                        timeout=timeout)

    def target(self, name):
        return os.path.join(self.tmp, name)

    def files_under(self, root):
        out = set()
        for dp, _, fns in os.walk(root):
            for fn in fns:
                out.add(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
        return out

    # ---- tests ----

    def test_download_filters_types_and_keeps_structure(self):
        t = self.target("t-basic")
        jid = self.new_job("download", "/docs", t)
        self.assertTrue(self.job(jid)["url"].endswith("/docs/"), "directory URL gets a trailing slash")
        j = self.wait_status(jid, "done", "failed")
        self.assertEqual(j["status"], "done", j)
        # saved relative to the start URL (/docs/), not under the server's full path
        self.assertEqual(self.files_under(t), {"a.pdf", "sub/c.PDF", "sub/deeper/d.pdf", "sp ace/ü file.pdf"})
        self.assertEqual((j["files_total"], j["counts"].get("done"), j["bytes_total"], j["bytes_done"]),
                         (4, 4, 6400, 6400))
        names = {f["path"] for f in self.files(jid)["files"]}
        self.assertIn("sp ace/ü file.pdf", names, "display path is decoded and relative to the start URL")

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_compare_with_missing_folder_lists_everything_without_downloading(self):
        t = self.target("t-cmp-none")
        jid = self.new_job("compare", "/docs/", t)
        j = self.wait_status(jid, "done", "failed")
        self.assertEqual((j["kind"], j["files_total"], j["bytes_total"], j["unknown"], j["broken"]),
                         ("compare", 4, 6400, 0, 0))
        self.assertEqual(j["counts"], {"missing": 4})
        self.assertEqual(j["missing_bytes"], 6400)
        self.assertIn("doesn't exist yet", j["note"])
        time.sleep(0.6)
        self.assertFalse(os.path.exists(t), "comparing must not download")
        self.call("/api/jobs/%d/download" % jid, {})
        j = self.wait_status(jid, "done", timeout=20)
        self.assertEqual(j["counts"], {"done": 4})
        self.assertEqual(len(self.files_under(t)), 4)

    def test_compare_classifies_local_partial_missing_and_downloads_only_the_gaps(self):
        t = self.target("t-cmp")
        src = lambda rel: open(os.path.join(self.root, rel), "rb").read()
        self.write(os.path.join(t, "a.pdf"), src("docs/a.pdf"))                           # in place
        self.write(os.path.join(t, "sub", "c.PDF"), src("docs/sub/c.PDF")[:700])          # cut short
        self.write(os.path.join(t, "flat", "D.PDF"), src("docs/sub/deeper/d.pdf"))        # elsewhere, same size
        self.write(os.path.join(t, "flat", "\u00fc file.pdf"), b"wrong size")             # name only: not a match
        jid = self.new_job("compare", "/docs/", t)
        j = self.wait_status(jid, "done", "failed")
        self.assertEqual(j["counts"], {"local": 2, "partial": 1, "missing": 1})
        self.assertEqual(j["missing_bytes"], (2000 - 700) + 400)
        self.assertEqual(j["note"], "")
        by_name = {f["name"]: f for f in self.files(jid)["files"]}
        self.assertEqual(by_name["d.pdf"]["status"], "local")
        self.assertEqual(by_name["d.pdf"]["local"].replace(os.sep, "/"), "flat/D.PDF")
        self.assertEqual(by_name["c.PDF"]["done"], 700)
        missing = self.files(jid, "missing")["files"]
        self.assertEqual([f["name"] for f in missing], ["\u00fc file.pdf"])

        self.fixture.ranges.clear()
        self.call("/api/jobs/%d/download" % jid, {})
        j = self.wait_status(jid, "done", timeout=20)
        self.assertEqual(j["counts"], {"local": 2, "done": 2})
        self.assertIn(("/docs/sub/c.PDF", "bytes=700-"), self.fixture.ranges, "incomplete file is resumed")
        self.assertEqual(open(os.path.join(t, "sub", "c.PDF"), "rb").read(), src("docs/sub/c.PDF"))
        self.assertEqual(open(os.path.join(t, "sp ace", "\u00fc file.pdf"), "rb").read(),
                         src("docs/sp ace/\u00fc file.pdf"))
        self.assertFalse(os.path.exists(os.path.join(t, "sub", "deeper", "d.pdf")),
                         "a file found elsewhere in the folder is not downloaded again")

    def test_compare_recognises_files_saved_in_the_old_full_path_layout(self):
        t = self.target("t-cmp-old")
        src = open(os.path.join(self.root, "docs", "a.pdf"), "rb").read()
        self.write(os.path.join(t, "docs", "a.pdf"), src)      # where earlier versions saved it
        jid = self.new_job("compare", "/docs/", t)
        self.wait_status(jid, "done")
        a = next(f for f in self.files(jid)["files"] if f["name"] == "a.pdf")
        self.assertEqual((a["status"], a["local"].replace(os.sep, "/")), ("local", "docs/a.pdf"))

    def test_non_ascii_names_download_and_compare_correctly(self):
        t = self.target("t-intl")
        jid = self.new_job("download", "/intl/", t)
        j = self.wait_status(jid, "done", "failed")
        self.assertEqual((j["status"], j["counts"], j["broken"]), ("done", {"done": 2}, 0), self.files(jid)["log"][-8:])
        self.assertEqual(self.files_under(t), {"Za\u017c\u00f3\u0142\u0107/g\u0119\u015bl\u0105 ja\u017a\u0144.pdf",
                                               "\u00fc \u041a\u0438\u0457\u0432.pdf"})
        cmp_id = self.new_job("compare", "/intl/", t)
        self.assertEqual(self.wait_status(cmp_id, "done")["counts"], {"local": 2})

    def test_raw_utf8_links_are_found(self):
        jid = self.new_job("compare", "/rawlinks/", self.target("t-raw"))
        j = self.wait_status(jid, "done", "failed")
        self.assertEqual((j["files_total"], j["broken"]), (3, 0), self.files(jid)["log"][-12:])
        self.assertEqual({f["path"] for f in self.files(jid)["files"]},
                         {"Genevi\u00e8ve.pdf", "It\u2019s.pdf", "\u6587\u4ef6/inner.pdf"})

    def test_garbled_links_are_repaired_found_and_downloaded(self):
        t = self.target("t-mangled")
        jid = self.new_job("compare", "/mangled/", t)
        j = self.wait_status(jid, "done", "failed", timeout=60)
        log = self.files(jid)["log"]
        paths = {f["path"] for f in self.files(jid)["files"]}
        self.assertEqual(paths, {"Genevi\u00e8ve.pdf", "It\u2019s.pdf", "plain.pdf", "\u6587\u4ef6/inner.pdf",
                                 "\u6587\u4ef6/Za\u017c\u00f3\u0142\u0107.pdf"}, log[-15:])
        self.assertEqual(j["bytes_total"], 111 + 222 + 333 + 555 + 444)
        broken = self.files(jid, "broken")["links"]
        self.assertEqual([(b["path"], b["status"][:3]) for b in broken], [("gon\u00e8.pdf", "404")],
                         "a link that fails even with its name corrected stays broken, under its real name")
        self.assertTrue(any("names corrected and retried" in l for l in log))
        self.call("/api/jobs/%d/download" % jid, {})
        j = self.wait_status(jid, "done", timeout=30)
        self.assertEqual(j["counts"], {"done": 5})
        self.assertEqual(self.files_under(t), paths)

    def test_complete_file_at_its_path_is_skipped_without_a_request(self):
        t = self.target("t-skip")
        self.write(os.path.join(t, "a.pdf"), open(os.path.join(self.root, "docs", "a.pdf"), "rb").read())
        self.fixture.gets.clear()
        jid = self.new_job("download", "/docs/", t)
        j = self.wait_status(jid, "done")
        self.assertEqual(j["counts"], {"done": 4})
        self.assertNotIn("/docs/a.pdf", self.fixture.gets)

    def test_failed_download_leaves_no_partial_files(self):
        src = os.path.join(self.root, "flaky", "x.pdf")
        t = self.target("t-fail-tidy")
        jid = self.new_job("compare", "/flaky/", t)
        self.wait_status(jid, "done")
        os.rename(src, src + ".hidden")
        try:
            self.call("/api/jobs/%d/download" % jid, {})
            wait_for(lambda: self.job(jid)["counts"].get("failed") == 1, timeout=15)
        finally:
            os.rename(src + ".hidden", src)
        self.assertEqual(self.files_under(t), set())
        self.assertFalse(os.path.exists(os.path.join(t, wget_gui.PARTIAL_DIR)))

    def test_download_one_missing_file(self):
        t = self.target("t-cmp-one")
        jid = self.new_job("compare", "/docs/", t)
        self.wait_status(jid, "done")
        target_file = next(f for f in self.files(jid)["files"] if f["name"] == "a.pdf")
        self.call("/api/jobs/%d/files/%d/resume" % (jid, target_file["id"]), {})
        wait_for(lambda: self.job(jid)["counts"] == {"done": 1, "missing": 3}, timeout=15)
        j = self.job(jid)
        self.assertEqual((j["kind"], j["status"]), ("compare", "done"))
        self.assertEqual(self.files_under(t), {"a.pdf"})

    def test_compare_again_sees_folder_changes(self):
        t = self.target("t-cmp-again")
        jid = self.new_job("compare", "/docs/", t)
        self.wait_status(jid, "done")
        self.assertEqual(self.job(jid)["counts"], {"missing": 4})
        self.write(os.path.join(t, "a.pdf"), open(os.path.join(self.root, "docs", "a.pdf"), "rb").read())
        self.call("/api/jobs/%d/rescan" % jid, {})
        wait_for(lambda: self.job(jid)["status"] == "done" and self.job(jid)["counts"] == {"local": 1, "missing": 3},
                 timeout=15)

    def test_rescan_of_downloaded_compare_job_queues_only_new_gaps(self):
        t = self.target("t-cmp-grow")
        d = os.path.join(self.root, "growing2")
        self.write(os.path.join(d, "one.pdf"), b"1" * 100)
        jid = self.new_job("compare", "/growing2/", t)
        self.wait_status(jid, "done")
        self.call("/api/jobs/%d/download" % jid, {})
        wait_for(lambda: self.job(jid)["counts"] == {"done": 1}, timeout=15)
        self.write(os.path.join(d, "two.pdf"), b"2" * 100)
        self.write(os.path.join(t, "elsewhere", "three.pdf"), b"3" * 100)
        self.write(os.path.join(d, "three.pdf"), b"3" * 100)
        self.call("/api/jobs/%d/rescan" % jid, {})
        wait_for(lambda: self.job(jid)["counts"] == {"done": 2, "local": 1}, timeout=15)
        self.assertNotIn("three.pdf", self.files_under(t))

    def test_all_in_file_types_means_every_type(self):
        for word in ("all", "*", "ALL"):
            jid = self.new_job("compare", "/docs/", self.target("t-all-" + word.strip("*") or "star"), types=word)
            j = self.wait_status(jid, "done", "failed")
            self.assertEqual((j["types"], j["files_total"]), ([], 5), word)   # 4 PDFs + notes.txt

    def test_no_match_for_file_types_explains_itself(self):
        jid = self.new_job("compare", "/docs/", self.target("t-epub"), types="epub")
        j = self.wait_status(jid, "done", "failed")
        self.assertEqual(j["files_total"], 0)
        self.assertIn("Clear the File types field", j["reason"])
        self.assertIn("epub", j["reason"])

    def test_log_lists_found_files_and_a_summary(self):
        jid = self.new_job("compare", "/docs/", self.target("t-log"))
        self.wait_status(jid, "done")
        log = self.files(jid)["log"]
        self.assertIn("Found: a.pdf (1000 B)", log)
        self.assertIn("Found: sub/deeper/d.pdf (2.9 KB)", log)
        self.assertTrue(any(l.startswith("Scan finished: 4 files") and l.endswith("file types: pdf") for l in log), log[-3:])

    def test_compare_lists_broken_links_with_server_response(self):
        jid = self.new_job("compare", "/broken/")
        j = self.wait_status(jid, "done", "failed")
        # sub/lost file.pdf: its folder doesn't exist, so no listing links to it (left out)
        self.assertEqual((j["files_total"], j["bytes_total"], j["broken"]), (1, 500, 2))
        page = self.files(jid, "broken")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["files"], [])
        got = {l["path"]: l for l in page["links"]}
        self.assertEqual(set(got), {"gone.pdf", "nodir/"})
        self.assertTrue(got["gone.pdf"]["url"].endswith("/broken/gone.pdf"))
        for l in page["links"]:
            self.assertTrue(l["status"].startswith("404"), l)
        # paging, and a rescan replaces the list rather than adding to it
        self.assertEqual(len(self.call("/api/jobs/%d/files?status=broken&limit=2" % jid)[1]["links"]), 2)
        self.call("/api/jobs/%d/rescan" % jid, {})
        wait_for(lambda: self.job(jid)["status"] == "done" and self.job(jid)["broken"] == 2, timeout=15)
        self.assertEqual(self.files(jid, "broken")["total"], 2)

    def test_dead_links_inside_saved_pages_are_left_out(self):
        jid = self.new_job("compare", "/pages/", self.target("t-pages"), types="")
        j = self.wait_status(jid, "done", "failed")
        log = self.files(jid)["log"]
        self.assertEqual(j["broken"], 0, log[-10:])
        self.assertTrue({"real name.pdf", "Saved_Page_files/logo.gif"}
                        <= {f["path"] for f in self.files(jid)["files"]})
        left = sorted(l.rsplit("/", 1)[-1] for l in log if l.startswith("Left out"))
        self.assertEqual(left, ["logo.gif", "old_name.pdf", "style.css"], log[-10:])
        self.assertTrue(any("3 dead links inside pages left out" in l for l in log), log[-3:])

    def test_scan_leaves_no_temp_files(self):
        before = {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("wget-gui-scan-")}
        jid = self.new_job("compare", "/docs/")
        self.wait_status(jid, "done", "failed")
        time.sleep(0.3)
        after = {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("wget-gui-scan-")}
        self.assertEqual(after - before, set())

    def test_parallel_limit_is_respected_and_applies_live(self):
        self.call("/api/settings", {"limit": 1})
        self.fixture.throttle = 0.06          # 512 KB = 8 chunks, about 0.5 s per file
        self.fixture.max_in_flight = 0
        jid = self.new_job("download", "/many/", self.target("t-par"))
        wait_for(lambda: self.job(jid)["counts"].get("active") == 1)
        time.sleep(1.0)
        self.assertEqual(self.fixture.max_in_flight, 1)
        self.call("/api/settings", {"limit": 4})
        wait_for(lambda: self.fixture.in_flight >= 3, timeout=10)
        j = self.wait_status(jid, "done", timeout=30)
        self.assertEqual(j["counts"], {"done": 6}, self.files(jid)["log"][-12:])
        self.assertLessEqual(self.fixture.max_in_flight, 4)
        self.assertGreaterEqual(self.fixture.max_in_flight, 3)

    def test_lowering_limit_lets_running_files_finish(self):
        self.call("/api/settings", {"limit": 4})
        self.fixture.throttle = 0.08
        jid = self.new_job("download", "/many/", self.target("t-lower"))
        wait_for(lambda: self.job(jid)["counts"].get("active") == 4)
        self.call("/api/settings", {"limit": 1})
        time.sleep(0.3)
        self.assertEqual(self.job(jid)["counts"].get("paused", 0), 0, "nothing gets paused")
        wait_for(lambda: self.job(jid)["counts"].get("active", 0) <= 1, timeout=15)
        j = self.wait_status(jid, "done", timeout=30)
        self.assertEqual(j["counts"], {"done": 6})

    def test_settings_are_clamped(self):
        self.assertEqual(self.call("/api/settings", {"limit": 99})[1]["limit"], wget_gui.LIMIT_MAX)
        self.assertEqual(self.call("/api/settings", {"limit": 0})[1]["limit"], wget_gui.LIMIT_MIN)
        self.assertEqual(self.call("/api/settings", {"limit": "x"})[0], 400)
        self.assertEqual(self.call("/api/state")[1]["limit"], wget_gui.LIMIT_MIN)

    def test_pause_and_resume_one_file_continues_with_range(self):
        t = self.target("t-file")
        dest = os.path.join(t, "big.pdf")
        part = lambda: glob.glob(os.path.join(t, wget_gui.PARTIAL_DIR, "*.part"))[0]
        self.fixture.throttle = 0.05          # ~3 s for 4 MB
        jid = self.new_job("download", "/slow/", t)
        active = lambda: [r for r in self.job(jid)["rows"] if r["status"] == "active"]
        wait_for(lambda: active() and active()[0]["done"] > 256 * 1024)
        f = active()[0]
        self.assertEqual(f["name"], "big.pdf")
        wait_for(lambda: active()[0]["speed"] > 0, timeout=5)
        self.call("/api/jobs/%d/files/%d/pause" % (jid, f["id"]), {})
        wait_for(lambda: self.files(jid)["files"][0]["status"] == "paused")
        self.assertEqual(self.job(jid)["status"], "paused")
        self.assertEqual([(r["name"], r["status"]) for r in self.job(jid)["rows"]], [("big.pdf", "paused")],
                         "a paused file stays visible under the job")
        self.assertFalse(os.path.exists(dest), "unfinished data stays in the partial file")
        partial = os.path.getsize(part())
        self.assertLess(partial, BIG)
        time.sleep(0.5)
        self.assertEqual(os.path.getsize(part()), partial, "wget really stopped")

        self.fixture.throttle = 0
        self.fixture.ranges.clear()
        self.call("/api/jobs/%d/files/%d/resume" % (jid, f["id"]), {})
        self.wait_status(jid, "done", timeout=20)
        self.assertIn(("/slow/big.pdf", "bytes=%d-" % partial), self.fixture.ranges,
                      "resume continued from the partial size instead of restarting")
        self.assertFalse(os.path.exists(os.path.join(t, wget_gui.PARTIAL_DIR)), "partial folder removed when empty")
        with open(dest, "rb") as a, open(self.big_src, "rb") as b:
            self.assertEqual(hashlib.sha256(a.read()).digest(), hashlib.sha256(b.read()).digest())

    def test_pause_and_resume_whole_job(self):
        self.call("/api/settings", {"limit": 2})
        self.fixture.throttle = 0.05
        t = self.target("t-job")
        jid = self.new_job("download", "/slowjob/", t)
        wait_for(lambda: self.job(jid)["counts"].get("active") == 2)
        self.call("/api/jobs/%d/pause" % jid, {})
        self.wait_status(jid, "paused")
        # The pause may land before the scan has listed every file; whatever is listed is paused.
        wait_for(lambda: set(self.job(jid)["counts"]) == {"paused"}, timeout=8)
        self.assertNotEqual(self.job(jid)["scan_state"], "scanning")
        wait_for(lambda: self.fixture.in_flight == 0, timeout=5)
        self.fixture.throttle = 0
        self.call("/api/jobs/%d/resume" % jid, {})
        j = self.wait_status(jid, "done", timeout=20)
        self.assertEqual(j["counts"], {"done": 4})
        for i in range(4):
            with open(os.path.join(t, "s%d.pdf" % i), "rb") as a, \
                    open(os.path.join(self.root, "slowjob", "s%d.pdf" % i), "rb") as b:
                self.assertEqual(a.read(), b.read())

    def test_failed_file_reports_reason_and_retry_works(self):
        src = os.path.join(self.root, "flaky", "x.pdf")
        jid = self.new_job("compare", "/flaky/", self.target("t-flaky"))
        self.wait_status(jid, "done")
        os.rename(src, src + ".hidden")       # file vanishes between scan and download
        try:
            self.call("/api/jobs/%d/download" % jid, {})
            wait_for(lambda: self.job(jid)["counts"].get("failed") == 1, timeout=15)
            f = self.files(jid, "failed")["files"][0]
            self.assertIn("404", f["error"])
            self.assertTrue(any("Failed: x.pdf" in line for line in self.files(jid)["log"]))
        finally:
            os.rename(src + ".hidden", src)
        self.call("/api/jobs/%d/retry" % jid, {})
        wait_for(lambda: self.job(jid)["counts"] == {"done": 1}, timeout=15)

    def test_rescan_picks_up_new_files(self):
        t = self.target("t-rescan")
        d = os.path.join(self.root, "growing")
        os.makedirs(d)
        with open(os.path.join(d, "one.pdf"), "wb") as f:
            f.write(b"1" * 100)
        jid = self.new_job("download", "/growing/", t)
        self.wait_status(jid, "done")
        with open(os.path.join(d, "two.pdf"), "wb") as f:
            f.write(b"2" * 100)
        self.call("/api/jobs/%d/rescan" % jid, {})
        wait_for(lambda: self.job(jid)["counts"] == {"done": 2}, timeout=15)
        self.assertEqual(self.files_under(t), {"one.pdf", "two.pdf"})

    def test_remove_stops_running_job(self):
        self.fixture.throttle = 0.05
        jid = self.new_job("download", "/slowjob/", self.target("t-remove"))
        wait_for(lambda: self.job(jid)["counts"].get("active"))
        self.call("/api/jobs/%d/remove" % jid, {})
        _, data = self.call("/api/state")
        self.assertNotIn(jid, [j["id"] for j in data["jobs"]])
        wait_for(lambda: self.fixture.in_flight == 0 and not self.app.active, timeout=10)

    def test_unreachable_server_fails_scan(self):
        code, data = self.call("/api/jobs", {"kind": "download", "url": "http://127.0.0.1:9/",
                                             "target": self.target("t-down")})
        j = self.wait_status(data["id"], "failed", "done")
        self.assertEqual(j["status"], "failed")
        self.assertIn("network failure", j["reason"])

    def test_rejects_bad_input(self):
        for body in ({"url": "example.com"}, {"url": "file:///etc/passwd"},
                     {"url": self.site + "/docs/", "types": "pdf;rm -rf"}, {"kind": "x", "url": self.site}):
            code, data = self.call("/api/jobs", body)
            self.assertEqual(code, 400, body)
            self.assertIn("error", data)
        self.assertEqual(self.call("/api/jobs/9999/files?status=bogus")[0], 404)
        self.assertEqual(self.call("/api/jobs/9999/pause", {})[0], 404)

    def test_pick_folder_endpoint(self):
        original = wget_gui.pick_folder
        try:
            seen = []
            wget_gui.pick_folder = lambda initial: seen.append(initial) or "/chosen/folder"
            self.assertEqual(self.call("/api/pick-folder", {"initial": "/start"}), (200, {"path": "/chosen/folder"}))
            self.assertEqual(seen, ["/start"])
            wget_gui.pick_folder = lambda initial: ""
            self.assertEqual(self.call("/api/pick-folder", {})[1], {"path": ""})
            wget_gui.pick_folder = lambda initial: None
            code, data = self.call("/api/pick-folder", {})
            self.assertEqual(code, 501)
            self.assertIn("Type the path", data["error"])
            self.assertEqual(self.call("/api/pick-folder", {}, token=False)[0], 403)
        finally:
            wget_gui.pick_folder = original

    def test_requires_token_and_local_host(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 403)
        self.assertEqual(self.call("/api/jobs", {"url": self.site + "/docs/"}, token=False)[0], 403)
        self.assertEqual(self.call("/api/settings", {"limit": 5}, token=False)[0], 403)
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.call("/api/state")[0], 200)


class UnitTest(unittest.TestCase):
    """Pieces checked without a server."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="wget-gui-unit-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.app = wget_gui.App("wget", self.d)
        self.addCleanup(self.app.shutdown)

    def test_same_name_without_known_size_is_not_a_match(self):
        os.makedirs(os.path.join(self.d, "other"))
        with open(os.path.join(self.d, "other", "report.pdf"), "wb") as f:
            f.write(b"x" * 10)
        job = wget_gui.Job("compare", "http://h/lib/", self.d, [])
        job.index = wget_gui.build_index(self.d)[1]
        unknown = wget_gui.FileItem(job, 0, "http://h/lib/2024/report.pdf", None)
        known = wget_gui.FileItem(job, 1, "http://h/lib/2023/report.pdf", 10)
        self.app._classify(unknown)
        self.app._classify(known)
        self.assertEqual((unknown.status, known.status), ("missing", "local"))

    def test_progress_reads_the_partial_file(self):
        job = wget_gui.Job("download", "http://h/lib/", self.d, [])
        f = wget_gui.FileItem(job, 0, "http://h/lib/a%20b.pdf", 100)
        f.dest = self.app._part_path(f)
        os.makedirs(os.path.dirname(f.dest))
        with open(f.dest, "wb") as out:
            out.write(b"y" * 42)
        with self.app.lock:
            self.app.active.append(f)
            self.app._measure()
            self.app.active.remove(f)
        self.assertEqual(f.done, 42)

    def test_interrupted_partial_counts_as_incomplete(self):
        job = wget_gui.Job("compare", "http://h/lib/", self.d, [])
        f = wget_gui.FileItem(job, 0, "http://h/lib/sub/x.pdf", 100)
        part = self.app._part_path(f)
        os.makedirs(os.path.dirname(part))
        with open(part, "wb") as out:
            out.write(b"z" * 30)
        self.app._classify(f)
        self.assertEqual((f.status, f.done), ("partial", 30))
        self.assertEqual(wget_gui.build_index(self.d)[1], {}, "partial files are not indexed as local files")

    def test_final_path_and_part_path(self):
        job = wget_gui.Job("download", "http://h/lib/", self.d, [])
        f = wget_gui.FileItem(job, 0, "http://h/lib/a%20b/c%C3%BC.pdf", 1)
        self.assertEqual(self.app._final_path(f), os.path.join(self.d, "a b", "c\u00fc.pdf"))
        part = self.app._part_path(f)
        self.assertEqual(os.path.dirname(part), os.path.join(self.d, wget_gui.PARTIAL_DIR))
        self.assertTrue(os.path.basename(part).isascii())
        self.assertEqual(part, self.app._part_path(wget_gui.FileItem(job, 5, f.url, 1)), "same URL, same partial")


@unittest.skipUnless(os.name == "posix", "uses a script stand-in for wget")
class ScanErrorTest(unittest.TestCase):
    """Scans that end with a wget error, using a stand-in wget that prints canned output."""

    FOUND = ("--2026-10-25 15:13:27--  http://h/vault/a.pdf\n"
             "HTTP request sent, awaiting response... 200 OK\n"
             "Length: 1234 [application/pdf]\n"
             "Remote file exists but does not contain any link -- not retrieving.\n")

    def run_scan(self, output, rc):
        d = tempfile.mkdtemp(prefix="wget-gui-fakewget-")
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "wget")
        with open(fake, "w") as f:
            f.write("#!%s\nimport sys\nsys.stderr.write(%r)\nsys.exit(%d)\n" % (sys.executable, output, rc))
        os.chmod(fake, 0o755)
        app = wget_gui.App(fake, os.path.join(d, "target"))
        self.addCleanup(app.shutdown)
        job = app.add_job("compare", "http://h/vault/", os.path.join(d, "target"), "pdf")
        wait_for(lambda: job.scan_state not in ("idle", "scanning"), timeout=10)
        with app.lock:
            return app._summary(job)

    def test_write_error_after_finding_files_keeps_the_result(self):
        tmp = os.path.join(tempfile.gettempdir(), "wget-gui-scan-abc")
        out = self.FOUND + (
            "%s/index.html.tmp.tmp: Bad file descriptor\n"
            "Cannot write to '%s/index.html.tmp.tmp' (Bad file descriptor).\n"
            "Found no broken links.\n\nFINISHED --2026-10-25 15:13:42--\n") % (tmp, tmp)
        j = self.run_scan(out, 3)
        self.assertEqual((j["status"], j["files_total"], j["bytes_total"], j["reason"]), ("done", 1, 1234, ""))
        self.assertIn("finished with errors", j["note"])
        self.assertIn("Bad file descriptor", j["note"])

    def test_error_before_finding_anything_fails_with_wgets_reason(self):
        out = ("--2026-10-25 13:19:10--  http://h/vault/\n"
               "Connecting to h:443... failed: Connection refused.\n"
               "Found no broken links.\n")
        j = self.run_scan(out, 4)
        self.assertEqual(j["status"], "failed")
        self.assertIn("network failure", j["reason"])
        self.assertIn("Connection refused", j["reason"])


class PackagingTest(unittest.TestCase):
    """Behaviour that matters for the frozen Windows executable."""

    def test_self_command(self):
        cmd = wget_gui.self_command("--pick-folder", "x")
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[-2:], ["--pick-folder", "x"])
        if not wget_gui.FROZEN:
            self.assertEqual(cmd[1], os.path.abspath(wget_gui.__file__))

    def test_tk_picker_relaunches_this_program(self):
        cmds = wget_gui.picker_commands("/start")
        tk = [c for c in cmds if "--pick-folder" in c[0]]
        self.assertEqual(bool(tk), wget_gui.have_tk())
        for argv, _, _ in tk:
            self.assertEqual(argv[-2:], ["--pick-folder", "/start"])

    def test_frozen_never_offers_powershell(self):
        orig = wget_gui.FROZEN
        wget_gui.FROZEN = True
        try:
            cmds = wget_gui.picker_commands("/start")
        finally:
            wget_gui.FROZEN = orig
        for argv, _, _ in cmds:
            self.assertNotIn("powershell", os.path.basename(argv[0]).lower())
            self.assertNotIn("pwsh", os.path.basename(argv[0]).lower())

    def test_frozen_wget_lookup_prefers_next_to_exe_then_bundled(self):
        d = tempfile.mkdtemp(prefix="wget-gui-frozen-")
        self.addCleanup(shutil.rmtree, d, True)
        exe_dir, bundle = os.path.join(d, "app"), os.path.join(d, "bundle")
        os.makedirs(exe_dir)
        os.makedirs(bundle)
        name = "wget.exe" if os.name == "nt" else "wget"
        for folder in (bundle,):
            with open(os.path.join(folder, name), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(folder, name), 0o755)
        saved = (wget_gui.FROZEN, sys.executable, getattr(sys, "_MEIPASS", None))
        try:
            wget_gui.FROZEN = True
            sys.executable = os.path.join(exe_dir, "wgmdownloader.exe")
            sys._MEIPASS = bundle
            self.assertEqual(wget_gui.find_wget(), os.path.join(bundle, name))
            with open(os.path.join(exe_dir, name), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(exe_dir, name), 0o755)
            self.assertEqual(wget_gui.find_wget(), os.path.join(exe_dir, name))
        finally:
            wget_gui.FROZEN, sys.executable = saved[0], saved[1]
            if saved[2] is None:
                del sys._MEIPASS
            else:
                sys._MEIPASS = saved[2]

    def test_check_flag(self):
        import subprocess
        r = subprocess.run(wget_gui.self_command("--check"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wget:", r.stdout)
        self.assertIn("Wget", r.stdout)

    @unittest.skipUnless(os.name == "nt", "job objects are Windows-only")
    def test_children_end_when_the_program_is_killed(self):
        import subprocess
        d = tempfile.mkdtemp(prefix="wget-gui-job-")
        self.addCleanup(shutil.rmtree, d, True)
        beat = os.path.join(d, "beat")
        child = "import time\nwhile True:\n    open(%r, 'a').write('.')\n    time.sleep(0.2)\n" % beat
        parent = ("import sys, time; sys.path.insert(0, %r); import wget_gui; "
                  "wget_gui.popen([sys.executable, '-c', %r]); time.sleep(60)"
                  % (os.path.dirname(os.path.abspath(wget_gui.__file__)), child))
        p = subprocess.Popen([sys.executable, "-c", parent])
        wait_for(lambda: os.path.exists(beat) and os.path.getsize(beat) > 3, timeout=20)
        p.kill()                       # like closing the window or ending the task
        p.wait()
        time.sleep(1.0)
        size = os.path.getsize(beat)
        time.sleep(1.5)
        self.assertEqual(os.path.getsize(beat), size, "the child kept running after the program died")


class PickerTest(unittest.TestCase):
    """pick_folder() with stand-in dialogs (python one-liners), so no real window opens."""

    def setUp(self):
        self._orig = wget_gui.picker_commands

    def tearDown(self):
        wget_gui.picker_commands = self._orig

    def fake(self, *cmds):
        wget_gui.picker_commands = lambda initial: list(cmds)

    def py(self, code):
        return [sys.executable, "-c", code]

    def test_returns_normalized_path(self):
        self.fake((self.py("print('/tmp/some/folder/')"), None, 1))
        self.assertEqual(wget_gui.pick_folder(""), os.path.normpath("/tmp/some/folder"))

    def test_cancel(self):
        self.fake((self.py("raise SystemExit(1)"), None, 1))
        self.assertEqual(wget_gui.pick_folder(""), "")
        self.fake((self.py("pass"), None, None))           # tk/PowerShell: empty output = cancel
        self.assertEqual(wget_gui.pick_folder(""), "")

    def test_broken_tool_falls_through_to_next(self):
        self.fake((["/nonexistent/picker"], None, 1),
                  (self.py("raise SystemExit(3)"), None, 1),
                  (self.py("import sys; sys.stdout.write(sys.stdin.read())"), "/from/stdin", None))
        self.assertEqual(wget_gui.pick_folder(""), os.path.normpath("/from/stdin"))

    def test_passes_initial_folder(self):
        self.fake((self.py("import os; print(os.environ['WGET_GUI_INITIAL'])"), None, None))
        self.assertEqual(wget_gui.pick_folder("/start/here"), os.path.normpath(os.path.abspath("/start/here")))

    def test_nothing_available(self):
        self.fake()
        self.assertIsNone(wget_gui.pick_folder(""))

    @unittest.skipUnless(sys.platform.startswith("linux"), "zenity is the Linux dialog")
    def test_linux_uses_zenity_from_path(self):
        d = tempfile.mkdtemp(prefix="wget-gui-fakebin-")
        try:
            z = os.path.join(d, "zenity")
            with open(z, "w") as f:
                f.write("#!/bin/sh\necho \"$@\" > %s/args\necho /picked/by/zenity\n" % d)
            os.chmod(z, os.stat(z).st_mode | stat.S_IEXEC)
            old = os.environ["PATH"]
            os.environ["PATH"] = d + os.pathsep + old
            try:
                wget_gui.picker_commands = self._orig
                self.assertEqual(wget_gui.pick_folder(d), "/picked/by/zenity")
                args = open(os.path.join(d, "args")).read()
                self.assertIn("--directory", args)
                self.assertIn("--filename=" + d + "/", args)
            finally:
                os.environ["PATH"] = old
        finally:
            shutil.rmtree(d, ignore_errors=True)


def unquote_path(url):
    from urllib.parse import unquote, urlparse
    return unquote(urlparse(url).path)


class ParserTest(unittest.TestCase):
    def test_spider_url_line_including_retries(self):
        r = wget_gui.SPIDER_URL_RE
        self.assertEqual(r.match("--2026-10-25 08:06:43--  http://h/a.pdf").group(1), "http://h/a.pdf")
        self.assertEqual(r.match("--2026-10-25 08:40:45-- (try: 2)  http://h/a.pdf").group(1), "http://h/a.pdf")
        self.assertEqual(r.match("--2026-10-25 08:40:45-- (try:12)  http://h/a.pdf").group(1), "http://h/a.pdf")

    def test_spider_status_line(self):
        m = wget_gui.SPIDER_STATUS_RE.search("HTTP request sent, awaiting response... 404 Not Found")
        self.assertEqual((m.group(1), m.group(2)), ("404", "Not Found"))
        m = wget_gui.SPIDER_STATUS_RE.search("HTTP request sent, awaiting response... 403 Forbidden ")
        self.assertEqual((m.group(1), m.group(2)), ("403", "Forbidden"))

    def test_http_error(self):
        m = wget_gui.HTTP_ERROR_RE.search("2026-10-25 08:33:52 ERROR 404: File not found.")
        self.assertEqual((m.group(1), m.group(2)), ("404", "File not found"))

    def test_progress_lines_are_recognised(self):
        self.assertTrue(wget_gui.PROGRESS_LINE_RE.match("  3072K ........ ........ 33% 1.20M 2s"))
        self.assertTrue(wget_gui.PROGRESS_LINE_RE.match("     0K ,,,,,,,, ........  5% 1.2M 9s"))
        self.assertFalse(wget_gui.PROGRESS_LINE_RE.match("Saving to: '/t/a.pdf'"))

    def test_normalize_url(self):
        n = wget_gui.normalize_url
        self.assertEqual(n("https://h/docs"), "https://h/docs/")
        self.assertEqual(n("https://h"), "https://h/")
        self.assertEqual(n("https://h/docs/"), "https://h/docs/")
        self.assertEqual(n("https://h/docs/a.pdf"), "https://h/docs/a.pdf")
        self.assertEqual(n("https://h/list?page=2"), "https://h/list?page=2")

    def test_parse_types(self):
        pt = wget_gui.parse_types
        for everything in ("", "  ", "all", "ALL", "any", "everything", "*", "*.*", "pdf, all", "pdf, *"):
            self.assertEqual(pt(everything), [], everything)
        self.assertEqual(pt("*.pdf .epub, PDF"), ["pdf", "epub"])
        self.assertEqual(pt("tar.gz; zip"), ["tar.gz", "zip"])
        with self.assertRaises(ValueError):
            pt("pdf;rm -rf")

    def test_listing_helpers(self):
        page = "http://h/a%20b//c/"
        html = ('<a href="../">up</a> <a href="x%20y.pdf">1</a> <a href="sub/">2</a> '
                '<a href="Genevi%C3%83%C2%A8ve.pdf">3</a> <a href="?C=N;O=D">sort</a>')
        keys = wget_gui.listing_keys(page, html)
        for url in ("http://h/a%20b/c/x%20y.pdf", "http://h/a b//c/x y.pdf", "http://h/a%20b//c/sub/",
                    "http://h/a%20b//c/Genevi%C3%A8ve.pdf"):
            self.assertIn(wget_gui.url_key(url), keys, url)
        self.assertNotIn(wget_gui.url_key("http://h/a%20b//c/x_y.pdf"), keys)
        self.assertEqual(wget_gui.parent_url("http://h/a/b%20c/d.pdf?x=1"), "http://h/a/b%20c/")
        self.assertEqual(wget_gui.parent_url("http://h/a/b/"), "http://h/a/")
        self.assertEqual(wget_gui.listing_keys(page, "<a href=broken"), set())

    def test_unmangle_url(self):
        u = wget_gui.unmangle_url
        fix = lambda url: unquote_path(u(url))
        self.assertEqual(fix("https://h/a/Vampire%20Genevi%C3%83%C2%A8ve/"), "/a/Vampire Genevi\u00e8ve/")
        self.assertEqual(fix("https://h/a/It%C3%A2%E2%82%AC%E2%84%A2s%20Your%20Funeral.pdf"), "/a/It\u2019s Your Funeral.pdf")
        self.assertEqual(fix("https://h/b/R_E_R_F%C3%A6%E2%80%93%E2%80%A1%C3%A4%C2%BB%C2%B6/"), "/b/R_E_R_F\u6587\u4ef6/")
        self.assertEqual(fix("https://h/a/Kh%C3%83%C2%A2rn.txt"), "/a/Kh\u00e2rn.txt")
        self.assertEqual(fix("https://h/G%C3%83%C2%A8ve/x/It%C3%A2%E2%82%AC%E2%84%A2s.pdf"), "/G\u00e8ve/x/It\u2019s.pdf")
        for fine in ("https://h/a/Genevi%C3%A8ve/", "https://h/plain/file.pdf",
                     "https://h/a/Za%C5%BC%C3%B3%C5%82%C4%87.pdf", "https://h/a/%E6%96%87%E4%BB%B6/"):
            self.assertIsNone(u(fine), fine)
        self.assertTrue(u("https://h/a/Genevi%C3%83%C2%A8ve.pdf").startswith("https://h/a/"))

    def test_safe_name(self):
        sn = wget_gui.safe_name
        self.assertEqual(sn("a b.pdf", windows=True), "a b.pdf")
        self.assertEqual(sn("Q: why?.pdf", windows=True), "Q_ why_.pdf")
        self.assertEqual(sn("dl.php?id=3", windows=True), "dl.php_id=3")
        self.assertEqual(sn("dl.php?id=3", windows=False), "dl.php?id=3")
        self.assertEqual(sn("con.pdf", windows=True), "_con.pdf")
        self.assertEqual(sn("trailing. ", windows=True), "trailing")
        self.assertEqual(sn("..", windows=False), "_")
        self.assertEqual(sn("g\u0119\u015bl\u0105.pdf", windows=True), "g\u0119\u015bl\u0105.pdf")

    def test_safe_dirs(self):
        sd = wget_gui.safe_dirs
        self.assertEqual(sd("a/b/c.pdf", windows=False), ["a", "b"])
        self.assertEqual(sd("c.pdf", windows=False), [])
        self.assertEqual(sd("../../etc/x.pdf", windows=False), ["_", "_", "etc"], "never climbs out of the folder")
        self.assertEqual(sd("Q: what?/x.pdf", windows=False), ["Q: what?"])
        self.assertEqual(sd("Q: what?/x.pdf", windows=True), ["Q_ what_"])
        self.assertEqual(sd("CON/nul.d/trailing. /x.pdf", windows=True), ["CON_", "nul.d_", "trailing"])
        self.assertEqual(sd("Ann's saved page_files/x", windows=True), ["Ann's saved page_files"])

    def test_scan_error_matcher(self):
        r = wget_gui.SCAN_ERROR_RE
        self.assertTrue(r.match("Cannot write to 'x/index.html.tmp' (Bad file descriptor)."))
        self.assertTrue(r.match("Connecting to h:443... failed: Connection refused."))
        self.assertTrue(r.match("ERROR: cannot verify h's certificate"))
        self.assertFalse(r.match("ERROR 404: Not Found."), "404s are broken links, not scan errors")
        self.assertFalse(r.match("Found no broken links."))

    def test_wget_options(self):
        self.assertIn("--no-directories", wget_gui.SCAN_ARGS)
        self.assertIn("--no-iri", wget_gui.SCAN_ARGS)
        self.assertIn("--no-iri", wget_gui.FILE_ARGS)
        self.assertNotIn("--force-directories", wget_gui.FILE_ARGS)

    def test_build_index(self):
        d = tempfile.mkdtemp(prefix="wget-gui-idx-")
        try:
            os.makedirs(os.path.join(d, "a", "b"))
            with open(os.path.join(d, "a", "b", "Report.PDF"), "wb") as f:
                f.write(b"x" * 10)
            exists, index = wget_gui.build_index(d)
            self.assertTrue(exists)
            self.assertEqual(index["report.pdf"], [(os.path.join(d, "a", "b", "Report.PDF"), 10)])
            self.assertEqual(wget_gui.build_index(os.path.join(d, "nope")), (False, {}))
        finally:
            shutil.rmtree(d)

    def test_relative_path(self):
        job = wget_gui.Job("download", "https://h/lib/", "/t", [])
        self.assertEqual(job.relative("https://h/lib/2024/a%20b.pdf"), "2024/a b.pdf")
        self.assertEqual(wget_gui.Job("download", "https://h/lib/one.pdf", "/t", []).relative(
            "https://h/lib/one.pdf"), "one.pdf")


if __name__ == "__main__":
    unittest.main()
