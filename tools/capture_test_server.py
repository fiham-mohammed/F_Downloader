"""Local web server for MANUAL testing of automatic PDF / image capture (Stage 6B).

Usage (from the project root, with the virtual environment):
    .venv\\Scripts\\python tools\\capture_test_server.py            # http://127.0.0.1:8765/
    .venv\\Scripts\\python tools\\capture_test_server.py --port 9000

Open the printed address in Chrome or Edge. The page links every case from the
README's "Manual testing guide". Files are padded to a few MB and sent slowly
(throttled), because on localhost a small file would otherwise finish before
any extension could pause it. Images stay valid: browsers ignore bytes after
the end-of-image marker. Binds to 127.0.0.1 only; serves generated data only.
"""
from __future__ import annotations

import argparse
import html
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter

PADDED_SIZE = 3 * 1024 * 1024
CHUNK = 64 * 1024
CHUNK_DELAY_S = 0.05     # ~1.3 MB/s: a 3 MB file takes a couple of seconds


def _image(fmt: str) -> bytes:
    img = QImage(480, 300, QImage.Format_RGB32)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, 480, 300)
    grad.setColorAt(0, QColor("#1d4ed8"))
    grad.setColorAt(1, QColor("#f59e0b"))
    p.fillRect(img.rect(), grad)  # no text: font rendering would need a QGuiApplication
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    if not img.save(buf, fmt):
        raise RuntimeError(f"Qt cannot encode {fmt}")
    return bytes(ba)


def _pdf(title: str) -> bytes:
    text = f"BT /F1 24 Tf 72 720 Td ({title}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _pad(data: bytes) -> bytes:
    return data + b"\0" * max(0, PADDED_SIZE - len(data))


PDF = _pad(_pdf("My Download Manager test PDF"))
JPG = _pad(_image("JPG"))
PNG = _pad(_image("PNG"))
WEBP = _pad(_image("WEBP"))
BMP = _pad(_image("BMP"))

# path -> (body, content type, Content-Disposition or None)
ROUTES = {
    "/files/report.pdf": (PDF, "application/pdf", 'attachment; filename="report.pdf"'),
    "/files/REPORT-UPPER.PDF": (PDF, "application/pdf", "attachment"),
    "/view/manual.pdf": (PDF, "application/pdf", None),                       # opens in the PDF viewer
    "/get": (PDF, "application/pdf", "attachment; filename*=UTF-8''Quarterly%20report%20%C3%A9t%C3%A9.pdf"),
    "/signed/report.pdf": (PDF, "application/pdf", "attachment"),
    "/images/photo.jpg": (JPG, "image/jpeg", None),
    "/images/photo.png": (PNG, "image/png", None),
    "/images/photo.webp": (WEBP, "image/webp", None),
    "/images/photo.bmp": (BMP, "image/bmp", None),
    "/images/attachment.jpg": (JPG, "image/jpeg", 'attachment; filename="holiday photo.jpg"'),
}

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>MDM capture test page</title>
<style>body{font:15px system-ui;margin:24px;max-width:860px}li{margin:6px 0}code{background:#eee;padding:1px 4px}
img{max-width:240px;border:1px solid #ccc;margin:4px}</style></head><body>
<h1>My Download Manager - PDF &amp; image capture test page</h1>
<p>Turn on Ask or Automatic capture in the extension popup first. See README &rsaquo; Manual testing guide.</p>
<h2>PDF</h2><ul>
<li>Case A, direct attachment: <a href="/files/report.pdf">report.pdf</a> (Content-Disposition: attachment)</li>
<li>Uppercase extension: <a href="/files/REPORT-UPPER.PDF">REPORT-UPPER.PDF</a></li>
<li>No extension in the URL: <a href="/get?id=7">/get?id=7</a> (name from Content-Disposition)</li>
<li>Signed query string: <a href="/signed/report.pdf?X-Amz-Signature=abc%2Fdef&amp;token=t0k&amp;name=x.exe">signed report.pdf</a></li>
<li>Redirect: <a href="/redirect/report">/redirect/report</a> &rarr; report.pdf</li>
<li>Case B, opens in the browser's PDF viewer (NOT captured automatically): <a href="/view/manual.pdf">manual.pdf</a>
  &mdash; then Case C: click the viewer's Download button. Right-click this link for the manual menu.</li>
<li><code>download</code> attribute: <a href="/view/manual.pdf" download>manual.pdf (download attribute)</a></li>
<li>Needs the browser session (stays in the browser): <a href="/private/payslip.pdf">payslip.pdf</a></li>
<li>POST form (never captured): <form method="post" action="/post/report.pdf" style="display:inline">
  <button>Download via POST</button></form></li>
</ul>
<h2>Images</h2>
<p><img src="/images/photo.jpg" alt="jpg"><img src="/images/photo.png" alt="png"></p><ul>
<li><a href="/images/photo.jpg" download>photo.jpg</a> (download attribute) or right-click an image &rsaquo; Save image as&hellip;</li>
<li><a href="/images/photo.png" download>photo.png</a> (download attribute)</li>
<li><a href="/images/photo.webp" download>photo.webp</a>, <a href="/images/photo.bmp" download>photo.bmp</a></li>
<li>Image as attachment: <a href="/images/attachment.jpg">attachment.jpg</a> (saved as "holiday photo.jpg")</li>
</ul></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, html.escape(self.path.split("?", 1)[0])))

    def _headers(self, status, ctype, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "max-age=3600")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _body(self, data, throttle=True):
        try:
            for i in range(0, len(data), CHUNK):
                self.wfile.write(data[i:i + CHUNK])
                if throttle:
                    time.sleep(CHUNK_DELAY_S)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # a paused / cancelled browser download, or MDM's 1-byte probe

    def _serve(self, data, ctype, cd):
        extra = {"Accept-Ranges": "bytes"}
        if cd:
            extra["Content-Disposition"] = cd
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s or 0)
            end = min(int(end_s) if end_s else len(data) - 1, len(data) - 1)
            extra["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            part = data[start:end + 1]
            self._headers(206, ctype, len(part), extra)
            return self._body(part, throttle=len(part) > 1)
        self._headers(200, ctype, len(data), extra)
        return self._body(data)

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            body = INDEX.encode("utf-8")
            # The cookie makes /private/payslip.pdf work in the browser only.
            self._headers(200, "text/html; charset=utf-8", len(body), {"Set-Cookie": "mdm_test_session=1; Path=/private"})
            return self._body(body, throttle=False)
        if path == "/redirect/report":
            self._headers(302, "text/plain", 0, {"Location": "/files/report.pdf"})
            return None
        if path == "/private/payslip.pdf":
            if "mdm_test_session=1" not in (self.headers.get("Cookie") or ""):
                body = b"Sign in first"
                self._headers(401, "text/plain", len(body), {"WWW-Authenticate": 'Basic realm="test"'})
                return self._body(body, throttle=False)
            return self._serve(PDF, "application/pdf", 'attachment; filename="payslip.pdf"')
        if path in ROUTES:
            return self._serve(*ROUTES[path])
        body = b"not found"
        self._headers(404, "text/plain", len(body))
        return self._body(body, throttle=False)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(min(length, 65536))
        return self._serve(PDF, "application/pdf", 'attachment; filename="posted-report.pdf"')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print(f"Capture test page: http://127.0.0.1:{args.port}/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
