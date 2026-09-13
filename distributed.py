"""Coordinate multiple devices validating a finite list of shared referral links."""

import argparse
import hmac
import json
import math
import os
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from curl_cffi import CurlOpt, requests
from curl_cffi.requests.exceptions import RequestException

from checker import MIN_COOLDOWN, retry_after_seconds
from referral_api import api_url, classify_api_response
from run import normalize_link

LEASE_SECONDS = 60
REQUEST_TIMEOUT = 10
MAX_BODY = 131072
TOKEN_ENV = "REFERRAL_COORDINATOR_TOKEN"


class Queue:
    def __init__(self, path, interval=3, workers=4, now=time.time):
        if not math.isfinite(interval) or interval < 2 or not 1 <= workers <= 16:
            raise ValueError("interval must be at least 2 seconds; workers must be 1–16")
        self.now = now
        self.workers = workers
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', lease TEXT, worker TEXT,
                expires REAL, attempts INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '', result_lease TEXT,
                result_status TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS control (
                id INTEGER PRIMARY KEY CHECK(id=1), interval REAL NOT NULL,
                next_start REAL NOT NULL, cooldown REAL NOT NULL,
                halted TEXT NOT NULL)""")
            self.db.execute("INSERT OR IGNORE INTO control VALUES (1, ?, 0, 0, '')", (interval,))
            self.db.execute("UPDATE control SET interval=MAX(interval, ?) WHERE id=1", (interval,))

    def close(self):
        self.db.close()

    def add(self, values):
        # Validate the complete input before importing anything.
        urls = list(dict.fromkeys(normalize_link(value) for value in values))
        with self.lock, self.db:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO jobs(url) VALUES (?)", ((url,) for url in urls))
            return self.db.total_changes - before

    def claim(self, worker):
        if not isinstance(worker, str) or not 1 <= len(worker) <= 100:
            raise ValueError("worker must be a nonempty string of at most 100 characters")
        with self.lock, self.db:
            now = self.now()
            self.db.execute("UPDATE jobs SET status='pending', lease=NULL, worker=NULL "
                            "WHERE status='leased' AND expires<=?", (now,))
            control = self.db.execute("SELECT * FROM control WHERE id=1").fetchone()
            if control["halted"]:
                return {"state": "halted", "reason": control["halted"]}
            remaining = self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'leased')").fetchone()[0]
            if not remaining:
                return {"state": "done"}
            delay = max(control["next_start"], control["cooldown"]) - now
            active = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='leased'").fetchone()[0]
            own = self.db.execute("SELECT 1 FROM jobs WHERE status='leased' AND worker=?", (worker,)).fetchone()
            if delay > 0 or active >= self.workers or own:
                return {"state": "wait", "seconds": min(60, max(1, delay))}
            job = self.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if job is None:
                return {"state": "wait", "seconds": 1}
            lease = secrets.token_hex(24)
            self.db.execute("UPDATE jobs SET status='leased', lease=?, worker=?, expires=?, "
                            "attempts=attempts+1 WHERE id=?", (lease, worker, now + LEASE_SECONDS, job["id"]))
            self.db.execute("UPDATE control SET next_start=? WHERE id=1", (now + control["interval"],))
            return {"state": "job", "id": job["id"], "url": job["url"], "lease": lease}

    def report(self, data):
        if (type(data.get("id")) is not int or not isinstance(data.get("lease"), str)
                or not isinstance(data.get("worker"), str)):
            raise ValueError("id, lease, and worker are required")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (data["id"],)).fetchone()
            if job is None:
                raise ValueError("unknown job")
            # Retrying a lost acknowledgement must not repeat validation or cooldowns.
            if job["result_lease"] == data["lease"] and job["worker"] == data["worker"]:
                return {"accepted": True, "status": job["result_status"], "duplicate": True}
            if (job["status"] != "leased" or job["lease"] != data["lease"]
                    or job["worker"] != data["worker"]):
                return {"accepted": False, "reason": "lease was reassigned"}
            if data.get("network_error") is True:
                status, detail = "network_error", "Worker could not complete the request."
                headers = {}
            else:
                raw = data.get("response")
                if (not isinstance(raw, dict) or type(raw.get("status_code")) is not int
                        or not isinstance(raw.get("url"), str) or not isinstance(raw.get("text"), str)
                        or not isinstance(raw.get("headers"), dict)
                        or any(not isinstance(k, str) or not isinstance(v, str)
                               for k, v in raw["headers"].items())):
                    raise ValueError("invalid response envelope")
                headers = {key.lower(): value for key, value in raw["headers"].items()}
                if "retry-after" in headers:
                    headers["Retry-After"] = headers["retry-after"]
                response = SimpleNamespace(**{**raw, "headers": headers})
                status, detail = classify_api_response(response, job["url"])
            now = self.now()
            control = self.db.execute("SELECT * FROM control WHERE id=1").fetchone()
            new_status = status if status in ("valid", "invalid") else "pending"
            if status == "rate_limited":
                delay = max(MIN_COOLDOWN, retry_after_seconds(headers.get("retry-after"), now=now) or 0)
                interval = control["interval"] * (2 if now >= control["cooldown"] else 1)
                self.db.execute("UPDATE control SET cooldown=MAX(cooldown, ?), interval=? WHERE id=1",
                                (now + delay, interval))
            elif status in ("error", "network_error") and job["attempts"] < 3:
                delay = max(2 ** job["attempts"], retry_after_seconds(headers.get("retry-after"), now=now) or 0)
                self.db.execute("UPDATE control SET next_start=MAX(next_start, ?) WHERE id=1", (now + delay,))
            elif status not in ("valid", "invalid"):
                self.db.execute("UPDATE control SET halted=? WHERE id=1", (f"{status}: {detail}",))
            self.db.execute("UPDATE jobs SET status=?, detail=?, result_lease=?, result_status=? WHERE id=?",
                            (new_status, detail, data["lease"], status, job["id"]))
            return {"accepted": True, "status": status, "detail": detail}

    def snapshot(self):
        with self.lock:
            counts = dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"))
            control = self.db.execute("SELECT * FROM control WHERE id=1").fetchone()
            remaining = max(0, control["cooldown"] - self.now())
            return {"counts": counts, "interval": control["interval"],
                    "cooldown_seconds": remaining if math.isfinite(remaining) else "indefinite",
                    "halted": control["halted"], "max_workers": self.workers}

    def results(self):
        with self.lock:
            return {"valid_links": [row[0] for row in self.db.execute(
                "SELECT url FROM jobs WHERE status='valid' ORDER BY id")]}


def make_server(queue, token, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def reply(self, code, payload):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self):
            supplied = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
                self.reply(401, {"error": "authentication required"})
                return
            try:
                if self.command == "GET" and self.path == "/status":
                    result = queue.snapshot()
                elif self.command == "GET" and self.path == "/results":
                    result = queue.results()
                elif self.command == "POST" and self.path in ("/claim", "/report"):
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                        raise ValueError("invalid body length")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("expected a JSON object")
                    result = queue.claim(data.get("worker")) if self.path == "/claim" else queue.report(data)
                else:
                    self.reply(404, {"error": "unknown endpoint"})
                    return
                self.reply(200, result)
            except (ValueError, UnicodeError) as exc:
                self.reply(400, {"error": str(exc)})
            except sqlite3.Error:
                self.reply(503, {"error": "queue storage unavailable"})

        do_GET = do_POST = dispatch

    # Remote workers connect using SSH forwarding; the API is never publicly bound.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = False
    return server


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base, token):
        parsed = urlsplit(base)
        if (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("use HTTPS or a localhost SSH tunnel for the coordinator")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("coordinator must be an origin URL without credentials, path, or query")
        self.base = base.rstrip("/")
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, path, data=None):
        request = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with self.opener.open(request, timeout=15) as response:
            return json.load(response)


def run_worker(client):
    worker = secrets.token_hex(12)

    def reliable_call(path, data):
        while True:
            try:
                return client.call(path, data)
            except HTTPError as exc:
                if exc.code < 500:
                    raise
            except (URLError, TimeoutError, ConnectionError):
                pass
            print("Coordinator unavailable; retrying in five seconds.", flush=True)
            time.sleep(5)

    with requests.Session(impersonate="chrome", curl_options={
        CurlOpt.TCP_KEEPALIVE: 1, CurlOpt.TCP_KEEPIDLE: 30,
        CurlOpt.TCP_KEEPINTVL: 15, CurlOpt.MAXAGE_CONN: 30,
    }) as session:
        while True:
            before = time.monotonic()
            job = reliable_call("/claim", {"worker": worker})
            if job["state"] == "done":
                print("All supplied links have been checked.", flush=True)
                return 0
            if job["state"] == "halted":
                print(f"Coordinator stopped: {job['reason']}", flush=True)
                return 2
            if job["state"] == "wait":
                print(f"Waiting {job['seconds']:g}s for shared pacing or another worker.", flush=True)
                time.sleep(job["seconds"])
                continue
            # A delayed grant must not cause a request using an expired lease.
            if time.monotonic() - before > LEASE_SECONDS - REQUEST_TIMEOUT - 5:
                continue
            url = normalize_link(job["url"])
            result = {"id": job["id"], "lease": job["lease"], "worker": worker}
            try:
                response = session.get(api_url(url), timeout=REQUEST_TIMEOUT, allow_redirects=False)
                result["response"] = {
                    "status_code": response.status_code, "url": response.url,
                    "text": response.text if len(response.text) <= 16000 else "",
                    "headers": {key: response.headers[key][:1000]
                                for key in ("Retry-After", "cf-mitigated") if key in response.headers},
                }
            except RequestException:
                result["network_error"] = True
            report = reliable_call("/report", result)
            if report["accepted"]:
                print(f"{report['status'].upper()}: {url}", flush=True)
            else:
                print("Result discarded: lease was reassigned.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("coordinator", help="import supplied links and serve the durable queue")
    serve.add_argument("--file", type=Path, required=True)
    serve.add_argument("--state", type=Path, default=Path("distributed.sqlite3"))
    serve.add_argument("--interval", type=float, default=3)
    serve.add_argument("--workers", type=int, default=4, help="global concurrent lease limit")
    serve.add_argument("--port", type=int, default=8765)
    for name in ("worker", "status", "results"):
        command = commands.add_parser(name)
        command.add_argument("--coordinator", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        parser.error(f"set {TOKEN_ENV} to the same random token (32+ ASCII characters, no whitespace) on each device")
    try:
        if args.command == "coordinator":
            if not 1 <= args.port <= 65535:
                parser.error("port must be 1–65535")
            values = [line.strip() for line in args.file.read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
            if not values:
                parser.error("no supplied links found")
            with closing(Queue(args.state, args.interval, args.workers)) as queue:
                # Bind before importing so a second coordinator on the same port fails cleanly.
                with make_server(queue, token, args.port) as server:
                    added = queue.add(values)
                    print(f"Imported {added} new link(s). Listening on 127.0.0.1:{args.port}.", flush=True)
                    print(json.dumps(queue.snapshot()), flush=True)
                    server.serve_forever()
        else:
            client = Client(args.coordinator, token)
            if args.command == "worker":
                return run_worker(client)
            result = client.call("/" + args.command)
            if args.command == "results":
                for url in result["valid_links"]:
                    print(url)
            else:
                print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print("\nStopped. The coordinator retains progress.")
        return 130
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
