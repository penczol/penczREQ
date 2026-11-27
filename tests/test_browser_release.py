"""Two real tabs and a native waiting worker, each run in a disposable Chrome profile."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import pytest
from websockets.sync.client import connect


STATIC = Path(__file__).resolve().parents[1] / "request_app" / "static"


def wait_for(read):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if value := read():
            return value
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {read}")


class Tab:
    def __init__(self, stack, port, url):
        request = Request(f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}", method="PUT")
        with urlopen(request, timeout=5) as response:
            endpoint = json.load(response)["webSocketDebuggerUrl"]
        self.socket = stack.enter_context(connect(endpoint, open_timeout=5))
        self.sequence = 0

    def evaluate(self, expression):
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": "Runtime.evaluate",
                                     "params": {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True}}))
        while True:
            message = json.loads(self.socket.recv(timeout=10))
            if message.get("id") == self.sequence:
                break
        assert "error" not in message, message
        result = message["result"]
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def ready(self, build):
        wait_for(lambda: self.evaluate("window.__penczreqPwaInitialized && document.documentElement.dataset.release")
                 == build)

    def refresh_status(self):
        self.evaluate("window.dispatchEvent(new Event('pageshow'))")

    def banner(self):
        return self.evaluate("!document.querySelector('[data-pwa-update]').hidden")


def test_real_browser_release_refresh_and_later_across_tabs(tmp_path):
    binary = shutil.which("google-chrome") or shutil.which("chromium")
    if not binary and sys.platform == "win32":
        chrome = Path(os.environ["ProgramFiles"]) / "Google/Chrome/Application/chrome.exe"
        binary = str(chrome) if chrome.is_file() else None
    if not binary:
        if os.environ.get("CI"):
            pytest.fail("Chrome is required for the CI browser regression")
        pytest.skip("Install Chrome or Chromium to run the browser regression")

    state = {"release": "1.0@a", "worker": 1, "pages": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                state["pages"] += 1
                release = state["release"]
                body = (f'<html data-release="{release}"><body class="user-view">'
                        '<aside hidden data-pwa-update><button data-apply-pwa-update></button>'
                        '<button data-dismiss-pwa-update></button></aside>'
                        f'<script src="/pwa.js?v={release}"></script></body></html>').encode()
                kind = "text/html"
            elif path == "/api/release":
                body = json.dumps({"release": state["release"]}).encode()
                kind = "application/json"
            elif path in ("/pwa.js", "/service-worker.js"):
                body = (STATIC / path[1:]).read_bytes()
                if path == "/service-worker.js":
                    body += f'\n// worker {state["worker"]}'.encode()
                kind = "application/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    with ExitStack() as stack:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        stack.callback(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.shutdown)
        proc = subprocess.Popen([binary, "--headless", "--disable-gpu", "--disable-dev-shm-usage",
                                 "--no-proxy-server", "--no-first-run", "--disable-extensions", "--disable-background-timer-throttling",
                                 "--disable-renderer-backgrounding", "--remote-debugging-port=0", f"--user-data-dir={tmp_path}", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            proc.terminate()
            proc.wait(timeout=15)
        stack.callback(stop)
        file = tmp_path / "DevToolsActivePort"
        port = wait_for(lambda: file.is_file() and file.read_text().splitlines()[0])
        url = f"http://127.0.0.1:{server.server_port}/"
        one = Tab(stack, port, url)
        one.ready("1.0@a")
        one.evaluate("navigator.serviceWorker.ready.then(() => true)")
        two = Tab(stack, port, url)
        two.ready("1.0@a")
        assert not one.banner() and not two.banner()
        state.update(release="1.0@b", worker=2)
        two.evaluate("navigator.serviceWorker.ready.then(r => r.update()).then(() => true)")
        wait_for(lambda: two.evaluate("navigator.serviceWorker.getRegistration().then(r => !!r.waiting)"))
        for tab in (one, two):
            tab.refresh_status()
            wait_for(tab.banner)
        one.evaluate("document.querySelector('[data-dismiss-pwa-update]').click()")
        wait_for(lambda: not two.banner())
        two.refresh_status()
        assert not one.banner() and not two.banner()

        # A normal browser reload brings in B even with another old tab open.
        two.evaluate("setTimeout(() => location.reload(), 0)")
        two.ready("1.0@b")
        wait_for(lambda: two.evaluate("navigator.serviceWorker.ready.then(r => !r.waiting)"))
        assert not two.banner()
        assert state["pages"] == 3

        # No worker change at all. Both tabs still detect C and reload once.
        state["release"] = "1.0@c"
        for tab in (one, two):
            tab.refresh_status()
            wait_for(tab.banner)
        one.evaluate("document.querySelector('[data-apply-pwa-update]').click()")
        for tab in (one, two):
            tab.ready("1.0@c")
            tab.refresh_status()
            tab.evaluate("navigator.serviceWorker.dispatchEvent(new Event('controllerchange'))")
            assert not tab.banner()
        assert state["pages"] == 5
