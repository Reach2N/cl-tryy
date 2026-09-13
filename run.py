"""Check Claude referral pages without treating HTTP 200 as proof of validity."""

import argparse
import asyncio
import itertools
import math
import re
import secrets
import sqlite3
import string
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from checker import SavedLinks, run_scan
from referral_api import api_url, classify_api_response

CHARACTERS = string.ascii_letters + string.digits + "-_"
BASE_URL = "https://claude.ai/referral/"
INVALID_PHRASES = (
    "referral link is no longer valid",
    "referral link has expired",
    "invalid referral link",
    "page not found",
)
CHALLENGE_PHRASES = ("just a moment", "verify you are human", "checking your browser")


class PageText(HTMLParser):
    """Read page text, excluding scripts and styles that may contain error strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.ignored.append(tag)

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def normalize_text(value):
    return " ".join(value.casefold().split())


def generate_slug():
    # All supplied examples are canonical, unpadded Base64url encodings of 7 bytes.
    return secrets.token_urlsafe(7)


def normalize_link(value):
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return BASE_URL + value
    parsed = urlsplit(value)
    if (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "claude.ai"
        and re.fullmatch(r"/referral/[A-Za-z0-9_-]+/?", parsed.path)
    ):
        return "https://claude.ai" + parsed.path.rstrip("/")
    raise ValueError(f"Expected a referral code or https://claude.ai/referral/CODE: {value}")


def classify_response(response, requested_url, match_text=None):
    status = response.status_code
    if status == 429:
        retry_after = response.headers.get("Retry-After")
        detail = f" Retry-After: {retry_after}." if retry_after else ""
        return "rate_limited", "HTTP 429; stopping." + detail
    if status in {401, 403}:
        return "blocked", f"HTTP {status}; access denied, stopping."
    if response.headers.get("cf-mitigated", "").casefold() == "challenge":
        return "blocked", "Browser challenge; stopping."
    if status in {404, 410}:
        return "invalid", f"HTTP {status}."
    if status != 200:
        return "error", f"Unexpected HTTP {status}; validity is unknown."

    parser = PageText()
    parser.feed(response.text)
    body = normalize_text(" ".join(parser.parts))
    if any(phrase in body for phrase in CHALLENGE_PHRASES):
        return "blocked", "Browser challenge; stopping."

    try:
        same_page = normalize_link(response.url) == requested_url
    except ValueError:
        same_page = False
    if not same_page:
        return "unknown", "Redirected away from this referral page; check it in a browser."

    if any(phrase in body for phrase in INVALID_PHRASES):
        return "invalid", "Page reports an invalid or expired link."
    if match_text and normalize_text(match_text) in body:
        return "candidate", "Matched the supplied text; confirm eligibility in a browser."
    return "unknown", "HTTP 200 alone cannot verify a referral; check it in a browser."


def run_checks(urls, *, interval=2.0, timeout=10.0, match_text=None,
               output=Path("candidate_links.txt"), random_search=False, use_api=False):
    attempts = 0
    saved = SavedLinks(output)
    started = time.monotonic()
    next_request = started
    print("Starting referral checks. Press Ctrl+C to stop.", flush=True)
    try:
        with requests.Session(impersonate="chrome") as session:
            for url in urls:
                # Request time counts toward the interval; no extra sleep after the last URL.
                remaining = next_request - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                next_request = time.monotonic() + interval
                attempts += 1
                try:
                    response = session.get(api_url(url) if use_api else url,
                                           timeout=timeout, allow_redirects=not use_api)
                except RequestException as exc:
                    print(f"[{attempts}] Network error: {exc}. Stopping.", file=sys.stderr)
                    return 2

                status, detail = (classify_api_response(response, url) if use_api
                                  else classify_response(response, url, match_text))
                print(f"[{attempts}] {status.upper()}: {url} — {detail}", flush=True)
                if status in {"blocked", "rate_limited", "error"}:
                    return 2
                if status in {"valid", "candidate"}:
                    label = "a server-validated link" if status == "valid" else "an unverified candidate"
                    if saved.add(url):
                        print(f"Saved {label} to {output}.")
                    else:
                        print(f"Already saved {label} in {output}.")
                    if random_search or status == "candidate":
                        return 0
                if use_api and status == "unknown":
                    return 1
                if status == "unknown" and random_search and not match_text:
                    print("Search stopped: these responses cannot establish which codes work.")
                    return 1
    finally:
        elapsed = time.monotonic() - started
        print(f"Checked {attempts} link(s) in {elapsed:.1f}s.", flush=True)
    return 0


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def finite_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("links", nargs="*", help="existing referral URLs or codes to check")
    parser.add_argument("--file", type=Path, help="UTF-8 file with one link or code per line")
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--max-attempts", type=positive_int, default=100,
                       help="maximum random guesses when no links are supplied (default: 100)")
    limit.add_argument("--loop", action="store_true",
                       help="check supplied links, then generate codes; stop on a valid code unless --collect is used")
    parser.add_argument("--collect", action="store_true",
                        help="save every valid link and continue")
    parser.add_argument("--wait-on-rate-limit", action="store_true",
                        help="pause all workers on HTTP 429, then retry after the saved cooldown")
    parser.add_argument("--adaptive", action="store_true",
                        help="gradually adjust pacing, remember rate-limited intervals, and wait on HTTP 429")
    parser.add_argument("--hours", type=finite_seconds,
                        help="stop after this many hours, saving progress when --state is used")
    parser.add_argument("--workers", type=positive_int, default=1,
                        help="concurrent requests sharing one interval (default: 1, maximum: 16)")
    parser.add_argument("--state", type=Path,
                        help="SQLite progress file; --loop defaults to search.sqlite3")
    parser.add_argument("--interval", type=finite_seconds,
                        help="seconds between request starts (default: 2; adaptive may slow down on 429)")
    parser.add_argument("--timeout", type=finite_seconds, default=10.0,
                        help="request timeout in seconds (default: 10)")
    parser.add_argument("--match-text", help="text from a known valid page; matches are unverified candidates")
    parser.add_argument("--output", type=Path,
                        help="append valid links here (default: found_code.txt)")
    args = parser.parse_args(argv)
    if args.interval is None:
        args.interval = 2.0
    if args.adaptive and args.interval < 2:
        parser.error("--adaptive requires an initial --interval of at least 2 seconds")
    if args.timeout == 0:
        parser.error("--timeout must be greater than zero")
    if args.match_text is not None and not args.match_text.strip():
        parser.error("--match-text must not be blank")
    if args.hours == 0:
        parser.error("--hours must be greater than zero")
    if args.workers > 16:
        parser.error("--workers must be at most 16")
    if args.match_text and (args.loop or args.collect or args.wait_on_rate_limit or args.adaptive
                           or args.workers > 1 or args.hours or args.state):
        parser.error("--match-text is only for single-worker HTML checks; omit it to use the validity API")

    supplied = list(args.links)
    if args.file is not None:
        try:
            supplied.extend(line.strip() for line in args.file.read_text(encoding="utf-8").splitlines()
                            if line.strip() and not line.lstrip().startswith("#"))
        except (OSError, UnicodeError) as exc:
            parser.error(str(exc))
    random_search = not args.links and args.file is None
    try:
        urls = list(dict.fromkeys(normalize_link(value) for value in supplied))
    except ValueError as exc:
        parser.error(str(exc))
    if not urls and not random_search:
        parser.error("no referral links supplied")
    if random_search or args.loop:
        print(f"Random search: {256 ** 7:,} possible codes in the observed format.\n"
              "Matching the format does not predict working codes; guessing remains impractical.")
        count = itertools.count() if args.loop else range(args.max_attempts)
        urls = itertools.chain(urls, (BASE_URL + generate_slug() for _ in count))
    output = args.output or Path("candidate_links.txt" if args.match_text else "found_code.txt")
    try:
        if (args.loop or args.collect or args.wait_on_rate_limit or args.adaptive or args.workers > 1
                or args.hours is not None or args.state):
            state_path = args.state or (Path("search.sqlite3")
                                        if args.loop or args.wait_on_rate_limit or args.adaptive else None)
            return asyncio.run(run_scan(urls, workers=args.workers, interval=args.interval,
                                        timeout=args.timeout, hours=args.hours,
                                        state_path=state_path, output=output,
                                        stop_on_valid=(random_search or args.loop) and not args.collect,
                                        wait_on_rate_limit=args.wait_on_rate_limit,
                                        adaptive=args.adaptive))
        return run_checks(urls, interval=args.interval, timeout=args.timeout,
                          match_text=args.match_text, output=output,
                          random_search=random_search, use_api=not args.match_text)
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 130
    except (OSError, UnicodeError, sqlite3.Error) as exc:
        print(f"File or system error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
