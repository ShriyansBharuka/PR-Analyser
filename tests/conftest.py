import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import linkwatch.cli  # noqa: F401

OK_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>linkwatch test page</title></head>"
    "<body><p>Everything is fine ✓</p></body></html>"
)

LATIN1_PAGE = (
    "<html><head><title>Café Crème</title></head><body>"
    "<h1>Bienvenue au café</h1>"
    "<p>Nous servons du thé, des crêpes et des gâteaux préparés "
    "à la française. Le café est ouvert de huit heures à dix-huit "
    "heures, sauf le dimanche. N'hésitez pas à réserver une table pour "
    "goûter notre crème brûlée et nos spécialités régionales.</p>"
    "</body></html>"
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_body(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self.send_body(200, OK_PAGE.encode("utf-8"), "text/html; charset=utf-8",
                           {"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"})
        elif self.path == "/missing":
            self.send_body(404, b"not found", "text/plain")
        elif self.path == "/redirect":
            self.send_body(302, b"", "text/plain", {"Location": "/ok"})
        elif self.path == "/slow":
            time.sleep(6)
            self.send_body(200, OK_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/latin1":
            self.send_body(200, LATIN1_PAGE.encode("cp1252"), "text/html")
        elif self.path == "/echo-ua":
            ua = self.headers.get("User-Agent", "")
            self.send_body(200, ua.encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self.send_body(404, b"not found", "text/plain")


@pytest.fixture(scope="session")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
