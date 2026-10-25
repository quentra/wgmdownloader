"""Black-box test of the built executable.

Usage: python ci/smoke_test.py unzipped/wgmdownloader/wgmdownloader.exe

CI runs it on the program unzipped from the release zip, so it tests what users get.

PATH is cut down to the system folder, so the program must use the wget bundled inside it.
Checks: --check reports a working wget; the page is served; a comparison against an empty
folder lists the files; downloading them produces byte-identical copies, including a file
whose folder and name contain an apostrophe and letters outside the Windows code page (Polish).
"""

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

EXE = os.path.abspath(sys.argv[1])
PORT = 8899
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Quiet(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass


def call(path, token, body=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), headers={"X-Token": token})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=10) as r:
        return json.loads(r.read())


def wait(pred, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(0.5)
    raise SystemExit("timed out waiting for " + what)


def main():
    root = tempfile.mkdtemp(prefix="wgm-smoke-")
    site = os.path.join(root, "site")
    folder = os.path.join(site, "vault", "Ann's \u00fcber Za\u017c\u00f3\u0142\u0107")
    os.makedirs(folder)
    pdf = os.urandom(300000)
    with open(os.path.join(folder, "Ann's \u00fc g\u0119\u015bl\u0105.pdf"), "wb") as f:
        f.write(pdf)
    with open(os.path.join(site, "vault", "readme.txt"), "w") as f:
        f.write("hello")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=site, **k))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/vault/" % srv.server_address[1]

    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    env = dict(os.environ, PATH=system, NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")

    check = subprocess.run([EXE, "--check"], env=env, capture_output=True, text=True, timeout=120)
    print(check.stdout.strip())
    if check.returncode != 0 or "Wget" not in check.stdout:
        raise SystemExit("--check failed: %s" % check.stderr)

    out = os.path.join(root, "out")
    proc = subprocess.Popen([EXE, "--no-browser", "--port", str(PORT)], env=env)
    try:
        page = wait(lambda: OPENER.open("http://127.0.0.1:%d/" % PORT, timeout=5).read().decode(), 90, "the page")
        token = re.search(r'const TOKEN = "([^"]+)"', page).group(1)

        job = call("/api/jobs", token, {"kind": "compare", "url": url, "target": out, "types": "all"})["id"]
        state = lambda: next(j for j in call("/api/state", token)["jobs"] if j["id"] == job)
        j = wait(lambda: (lambda s: s if s["status"] == "done" else None)(state()), 120, "the comparison")
        print("compare:", j["counts"], j["bytes_total"], "bytes")
        if j["counts"] != {"missing": 2} or j["broken"]:
            raise SystemExit("expected 2 missing files, got %r (log: %r)"
                             % (j["counts"], call("/api/jobs/%d/files" % job, token)["log"][-10:]))

        call("/api/jobs/%d/download" % job, token, {})
        j = wait(lambda: (lambda s: s if s["status"] == "done" else None)(state()), 120, "the download")
        print("download:", j["counts"])
        if os.path.exists(os.path.join(out, ".wgm-partial")):
            raise SystemExit("partial folder left behind")
        got = os.path.join(out, "Ann's \u00fcber Za\u017c\u00f3\u0142\u0107", "Ann's \u00fc g\u0119\u015bl\u0105.pdf")
        with open(got, "rb") as f:
            if f.read() != pdf:
                raise SystemExit("downloaded file differs")
        with open(os.path.join(out, "readme.txt")) as f:
            if f.read() != "hello":
                raise SystemExit("readme.txt differs")
        print("smoke test passed")
    finally:
        proc.terminate()
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
