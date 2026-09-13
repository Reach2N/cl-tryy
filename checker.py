"""Concurrent referral checks with shared pacing and durable progress."""

import asyncio
import itertools
import math
import re
import sqlite3
import time
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from curl_cffi import CurlOpt, requests
from curl_cffi.requests.exceptions import RequestException

from referral_api import api_url, classify_api_response

MIN_COOLDOWN = 300.0
MIN_ADAPTIVE_INTERVAL = 2.0
ADAPTIVE_WINDOW = 30


def retry_after_seconds(value, now=None):
    """Parse either Retry-After delay-seconds or an HTTP date."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            # A delay too large for a float means indefinitely waiting, never an early retry.
            return float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - (time.time() if now is None else now)
    except (ValueError, TypeError, OverflowError):
        return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class SavedLinks:
    """Append new results without duplicating links saved by an earlier run."""

    def __init__(self, path):
        self.path = Path(path)
        self.urls = None
        self.needs_newline = False

    def add(self, url):
        if self.urls is None:
            try:
                text = self.path.read_text(encoding="utf-8")
                self.urls = set(text.splitlines())
                self.needs_newline = bool(text and not text.endswith(("\n", "\r")))
            except FileNotFoundError:
                self.urls = set()
        if url in self.urls:
            return False
        with self.path.open("a", encoding="utf-8") as handle:
            if self.needs_newline:
                handle.write("\n")
            handle.write(url + "\n")
        self.needs_newline = False
        self.urls.add(url)
        return True


class SearchState:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        try:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS checks (
                    url TEXT PRIMARY KEY, status TEXT NOT NULL,
                    checked_at TEXT NOT NULL, detail TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS cooldown (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    not_before REAL NOT NULL, backoff REAL NOT NULL,
                    request_interval REAL NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS adaptive_pacing (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    minimum_interval REAL NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS pacing_progress (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    request_interval REAL NOT NULL,
                    accepted INTEGER NOT NULL
                )
            """)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def is_invalid(self, url):
        row = self.db.execute("SELECT status FROM checks WHERE url = ?", (url,)).fetchone()
        return row is not None and row[0] == "invalid"

    def unfinished(self):
        return [row[0] for row in self.db.execute(
            "SELECT url FROM checks WHERE status NOT IN ('valid', 'invalid') ORDER BY checked_at"
        )]

    def record(self, url, status, detail, attempted=False):
        self.db.execute("""
            INSERT INTO checks (url, status, checked_at, detail, attempts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status = excluded.status, checked_at = excluded.checked_at,
                detail = excluded.detail, attempts = checks.attempts + excluded.attempts
        """, (url, status, datetime.now(timezone.utc).isoformat(), detail, int(attempted)))

    def total_attempts(self):
        return self.db.execute("SELECT COALESCE(SUM(attempts), 0) FROM checks").fetchone()[0]

    def save_cooldown(self, not_before, backoff, interval):
        self.db.execute("DELETE FROM pacing_progress")
        self.db.execute("INSERT OR REPLACE INTO cooldown VALUES (1, ?, ?, ?)",
                        (not_before, backoff, interval))

    def load_cooldown(self):
        return self.db.execute(
            "SELECT not_before, backoff, request_interval FROM cooldown WHERE id = 1"
        ).fetchone()

    def save_adaptive_floor(self, interval):
        self.db.execute("INSERT OR REPLACE INTO adaptive_pacing VALUES (1, ?)", (interval,))

    def load_adaptive_floor(self):
        row = self.db.execute(
            "SELECT minimum_interval FROM adaptive_pacing WHERE id = 1"
        ).fetchone()
        return row[0] if row else MIN_ADAPTIVE_INTERVAL

    def save_pacing_progress(self, pacer):
        self.db.execute("INSERT OR REPLACE INTO pacing_progress VALUES (1, ?, ?)",
                        (pacer.interval, pacer.accepted))

    def load_pacing_progress(self):
        return self.db.execute(
            "SELECT request_interval, accepted FROM pacing_progress WHERE id = 1"
        ).fetchone()


class RequestPacer:
    def __init__(self, interval, adaptive=False):
        self.adaptive = adaptive
        self.minimum_interval = MIN_ADAPTIVE_INTERVAL if adaptive else 0.0
        self.interval = max(interval, self.minimum_interval)
        self.accepted = 0
        self.generation = 0
        self.next_start = 0.0
        self.cooldown_until = 0.0
        self.backoff = 0.0
        self.lock = asyncio.Lock()

    def defer_rate_limit(self, retry_after, request_interval=None):
        now = time.monotonic()
        # Every episode uses the same local wait; server deadlines can extend it.
        self.backoff = MIN_COOLDOWN
        if now >= self.cooldown_until:
            self.generation += 1
            if self.adaptive:
                # Keep headroom above the interval that failed; never probe it again.
                failed_interval = max(self.interval, request_interval or 0.0)
                self.minimum_interval = max(self.minimum_interval, failed_interval * 1.25)
                # Adaptive mode keeps the measured relationship to the failed rate;
                # the regular mode retains its conservative ten-second floor.
                self.interval = max(self.minimum_interval, failed_interval * 2)
            else:
                self.interval = max(self.interval, min(60.0, max(10.0, self.interval * 2)))
            self.next_start = max(self.next_start, now + self.interval)
        elif self.adaptive and request_interval is not None:
            # A late response may describe an earlier, slower rate than today's setting.
            self.minimum_interval = max(self.minimum_interval, request_interval * 1.25)
            self.interval = max(self.interval, self.minimum_interval)
            self.next_start = max(self.next_start, now + self.interval)
        self.accepted = 0
        delay = max(self.backoff, retry_after_seconds(retry_after) or 0.0)
        self.cooldown_until = max(self.cooldown_until, now + delay)
        return self.cooldown_until - now

    def restore_cooldown(self, not_before, backoff, interval):
        remaining = max(0.0, not_before - time.time())
        self.cooldown_until = time.monotonic() + remaining
        # Preserve the saved deadline, but discard legacy escalating backoff.
        self.backoff = min(backoff, MIN_COOLDOWN)
        self.interval = max(self.interval, interval)
        return remaining

    def restore_adaptive_floor(self, interval):
        self.minimum_interval = max(self.minimum_interval, interval)
        self.interval = max(self.interval, self.minimum_interval)

    def observe(self, status, generation):
        """Speed up only after a full window of accepted checks in this generation."""
        if not self.adaptive or generation != self.generation:
            return False
        if status not in ("valid", "invalid") or time.monotonic() < self.cooldown_until:
            self.accepted = 0
            return False
        self.accepted += 1
        if self.accepted < ADAPTIVE_WINDOW:
            return False
        self.accepted = 0
        interval = max(self.minimum_interval, self.interval * 0.9)
        if interval >= self.interval:
            return False
        self.interval = interval
        self.generation += 1
        # Already queued starts may retain their longer gap; never introduce a burst.
        return True

    async def wait(self, stop):
        async with self.lock:
            while not stop.is_set():
                delay = max(self.next_start, self.cooldown_until) - time.monotonic()
                if delay <= 0:
                    self.next_start = time.monotonic() + self.interval
                    return True
                await asyncio.sleep(min(delay, 60.0))
                # A response may extend the cooldown while this worker is sleeping.
            return False


async def run_scan(urls, *, workers=4, interval=2.0, timeout=10.0,
                   hours=None, state_path=None, output=Path("found_code.txt"),
                   stop_on_valid=False, retries=2, wait_on_rate_limit=False,
                   adaptive=False):
    started = time.monotonic()
    deadline = started + hours * 3600 if hours is not None else None
    stop = asyncio.Event()
    pacer = RequestPacer(interval, adaptive=adaptive)
    wait_on_rate_limit = wait_on_rate_limit or adaptive
    attempts = 0
    valid_count = 0
    new_count = 0
    saved = SavedLinks(output)
    pending = set()
    seen = set()
    exit_code = 0
    context = closing(SearchState(state_path)) if state_path is not None else nullcontext(None)
    with context as state:
        source = iter(itertools.chain(state.unfinished() if state else (), urls))
        if state and (cooldown := state.load_cooldown()):
            remaining = pacer.restore_cooldown(*cooldown)
            if remaining:
                print(f"Saved rate limit: waiting at least {remaining:.0f}s before any request.", flush=True)
        if state and adaptive:
            pacer.restore_adaptive_floor(state.load_adaptive_floor())
            progress = state.load_pacing_progress()
            if progress and pacer.cooldown_until <= time.monotonic():
                saved_interval, accepted = progress
                pacer.interval = max(interval, pacer.minimum_interval, saved_interval)
                if pacer.interval == saved_interval:
                    pacer.accepted = accepted
        print(f"Starting {workers} worker(s); one request start per {pacer.interval:g}s globally.", flush=True)
        if adaptive:
            print(f"Adaptive pacing: reduce the interval by 10% after {ADAPTIVE_WINDOW} accepted checks; "
                  f"minimum {pacer.minimum_interval:g}s. HTTP 429 raises this minimum.", flush=True)
            print(f"Adaptive progress: {pacer.accepted}/{ADAPTIVE_WINDOW}; "
                  "current pace and progress are saved after each response when state is enabled.", flush=True)
        if state:
            print(f"Resuming {state_path}: {state.total_attempts()} earlier request(s).", flush=True)
        if hours is not None:
            print(f"Time limit: {hours:g} hour(s).", flush=True)

        async with requests.AsyncSession(
            impersonate="chrome", max_clients=workers,
            curl_options={
                CurlOpt.TCP_KEEPALIVE: 1,
                CurlOpt.TCP_KEEPIDLE: 30,
                CurlOpt.TCP_KEEPINTVL: 15,
                CurlOpt.MAXAGE_CONN: 30,
            },
        ) as session:
            async def check(url):
                nonlocal attempts, valid_count, new_count
                if state:
                    state.record(url, "pending", "Waiting to check.")
                retry = 0
                while True:
                    if not await pacer.wait(stop):
                        return "cancelled"
                    if deadline is not None and time.monotonic() >= deadline:
                        return "cancelled"
                    attempts += 1
                    number = attempts
                    request_timeout = timeout
                    if deadline is not None:
                        request_timeout = min(timeout, max(0.001, deadline - time.monotonic()))
                    if state:
                        state.record(url, "pending", "Request started.", attempted=True)
                    generation = pacer.generation
                    request_interval = pacer.interval
                    try:
                        response = await session.get(api_url(url), timeout=request_timeout,
                                                     allow_redirects=False)
                        status, detail = classify_api_response(response, url)
                    except RequestException as exc:
                        status, detail = "network_error", str(exc)
                    if status == "rate_limited":
                        delay = pacer.defer_rate_limit(response.headers.get("Retry-After"),
                                                      request_interval=request_interval)
                        if state:
                            state.save_cooldown(time.time() + delay, pacer.backoff, pacer.interval)
                            if adaptive:
                                state.save_adaptive_floor(pacer.minimum_interval)
                        if wait_on_rate_limit:
                            detail = detail.replace("stopping.", "waiting for the shared cooldown.")
                    elif pacer.observe(status, generation):
                        print(f"Adaptive pacing: {ADAPTIVE_WINDOW} accepted checks; "
                              f"interval now {pacer.interval:.3f}s "
                              f"({60 / pacer.interval:.1f} request starts/minute maximum).", flush=True)
                    if state:
                        state.record(url, status, detail)
                        if adaptive:
                            state.save_pacing_progress(pacer)
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"{stamp} [{number}] {status.upper()}: {url} — {detail}", flush=True)
                    if status == "rate_limited" and wait_on_rate_limit:
                        print(f"All new requests paused for at least {delay:.0f}s; "
                              f"resuming at one request start per {pacer.interval:g}s. "
                              "This code will be retried.", flush=True)
                        if adaptive:
                            print(f"Adaptive minimum interval is now {pacer.minimum_interval:g}s; "
                                  "future speed increases will keep this headroom.", flush=True)
                        continue
                    if status in ("network_error", "error") and retry < retries:
                        delay = min(30.0, 2.0 ** (retry + 1))
                        if status == "error":
                            delay = max(delay, retry_after_seconds(
                                response.headers.get("Retry-After")) or 0.0)
                        print(f"Retrying this code in {delay:g}s ({retry + 1}/{retries}).", flush=True)
                        retry += 1
                        await asyncio.sleep(delay)
                        continue
                    if status == "valid":
                        valid_count += 1
                        if saved.add(url):
                            new_count += 1
                            print(f"VALID LINK saved to {output}.", flush=True)
                        else:
                            print(f"VALID LINK already saved in {output}.", flush=True)
                        if stop_on_valid:
                            stop.set()
                    elif status in ("blocked", "rate_limited", "unknown", "network_error", "error"):
                        stop.set()
                    return status

            def schedule():
                while len(pending) < workers and not stop.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    try:
                        url = next(source)
                    except StopIteration:
                        return
                    if url in seen or (state and state.is_invalid(url)):
                        continue
                    seen.add(url)
                    pending.add(asyncio.create_task(check(url)))

            try:
                schedule()
                while pending:
                    remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
                    done, pending = await asyncio.wait(
                        pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                    if not done:
                        print("Time limit reached; progress saved.", flush=True)
                        break
                    for status in await asyncio.gather(*done, return_exceptions=True):
                        if isinstance(status, BaseException):
                            raise status
                        if status in ("blocked", "rate_limited", "network_error", "error"):
                            exit_code = 2
                        elif status == "unknown" and exit_code == 0:
                            exit_code = 1
                    if stop.is_set():
                        break
                    schedule()
            finally:
                stop.set()
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                elapsed = time.monotonic() - started
                print(f"Finished: {attempts} request(s), {valid_count} valid link(s), "
                      f"{new_count} newly saved, {elapsed:.1f}s elapsed.", flush=True)
                if state:
                    print(f"Progress: {state_path} ({state.total_attempts()} total request(s)).", flush=True)
    return exit_code
