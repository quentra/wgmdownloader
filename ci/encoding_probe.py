"""Temporary diagnostic (Windows CI): which wget options request raw-UTF-8 links correctly?

A local server lists files with unescaped UTF-8 hrefs (as many real servers do), declared
three ways. Each wget variant scans it; the requested paths are reported as annotations.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote

sys.path.insert(0, os.getcwd())
import wget_gui  # noqa: E402

NAMES = ["Geneviève.pdf", "It’s.pdf", "plain.pdf"]
seen = []


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.go(False)

    def do_GET(self):
        self.go(True)

    def go(self, body):
        seen.append(self.path)
        parts = self.path.strip("/").split("/", 1)
        variant = parts[0]
        if self.path.endswith("/") and len(parts) == 1:
            meta = '<meta charset="utf-8">' if variant == "meta" else ""
            html = "<html><head>%s</head><body>%s</body></html>" % (
                meta, " ".join('<a href="%s">x</a>' % n for n in NAMES))
            data = html.encode("utf-8")
            ctype = {"utf8hdr": "text/html; charset=utf-8"}.get(variant, "text/html")
            self.send_response(200)
        elif len(parts) == 2 and unquote(parts[1]) in NAMES:
            data, ctype = b"x" * 10, "application/pdf"
            self.send_response(200)
        else:
            data, ctype = b"", "text/plain"
            self.send_response(404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d/" % srv.server_address[1]
wget = wget_gui.find_wget()
plain = [a for a in wget_gui.SCAN_ARGS if a != "--no-iri"]
variants = {
    "no-iri (current)": wget_gui.SCAN_ARGS,
    "iri default": plain,
    "iri local+remote utf8": plain + ["--local-encoding=UTF-8", "--remote-encoding=UTF-8"],
    "iri remote utf8": plain + ["--remote-encoding=UTF-8"],
    "iri local utf8": plain + ["--local-encoding=UTF-8"],
}
for name, args in variants.items():
    result = {}
    for page in ("nohdr", "utf8hdr", "meta"):
        del seen[:]
        r = subprocess.run([wget] + args + [base + page + "/"], capture_output=True,
                           cwd=tempfile.mkdtemp(), env=wget_gui.wget_env())
        broken = r.stderr.decode("utf-8", "replace").count("Remote file does not exist")
        reqs = sorted(set(p.split("/", 2)[2] for p in seen if p.count("/") >= 2 and p.endswith(".pdf")))
        result[page] = {"broken": broken, "requested": reqs}
    print("::warning title=%s::%s" % (name, json.dumps(result, ensure_ascii=True)), flush=True)
