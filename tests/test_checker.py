import asyncio
import base64
import contextlib
import io
import json
import tempfile
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import checker
import run
from referral_api import api_url, classify_api_response

BASE = "https://claude.ai/referral/"
URLS = [BASE + code for code in ("EMvVYtsAog", "CIV1gruvBg", "DG2BFZM8Qg", "EU9Kmhc76A")]


def response(url, payload=None, status=200, text=None, headers=None):
    return SimpleNamespace(url=api_url(url), status_code=status,
                           text=json.dumps(payload) if text is None else text,
                           headers=headers or {})


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.real_sleep = asyncio.sleep
        self.delays = []

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now

    async def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay
        await self.real_sleep(0)


class RetryAfterTests(unittest.TestCase):
    def test_seconds_dates_and_past_dates(self):
        self.assertEqual(checker.retry_after_seconds(" 900 "), 900)
        self.assertEqual(checker.retry_after_seconds(formatdate(1900, usegmt=True), now=1000), 900)
        self.assertEqual(checker.retry_after_seconds(formatdate(900, usegmt=True), now=1000), 0)
        self.assertEqual(checker.retry_after_seconds("9" * 400), float("inf"))

    def test_malformed_headers_use_fallback(self):
        for value in (None, "", "-1", "1.5", "nan", "infinity", "tomorrow"):
            with self.subTest(value=value):
                self.assertIsNone(checker.retry_after_seconds(value))


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_adaptive_requires_sustained_accepted_responses_and_has_a_rate_cap(self):
        pacer = checker.RequestPacer(10, adaptive=True)
        for _ in range(29):
            self.assertFalse(pacer.observe("invalid", pacer.generation))
        self.assertFalse(pacer.observe("error", pacer.generation))
        for _ in range(29):
            self.assertFalse(pacer.observe("valid", pacer.generation))
        self.assertEqual(pacer.interval, 10)
        self.assertTrue(pacer.observe("invalid", pacer.generation))
        self.assertEqual(pacer.interval, 9)
        for _ in range(2000):
            pacer.observe("invalid", pacer.generation)
        self.assertEqual(pacer.interval, 2)
        self.assertEqual(checker.RequestPacer(0, adaptive=True).interval, 2)

    async def test_adaptive_keeps_headroom_and_excludes_old_inflight_responses(self):
        clock = FakeTime()
        pacer = checker.RequestPacer(8, adaptive=True)
        generation = pacer.generation
        with patch.object(checker, "time", clock):
            self.assertEqual(pacer.defer_rate_limit(None), 300)
            self.assertEqual(pacer.interval, 16)
            self.assertEqual(pacer.minimum_interval, 10)
            clock.now = 1
            self.assertEqual(pacer.defer_rate_limit("900", request_interval=8), 900)
            self.assertEqual(pacer.interval, 16)
            for _ in range(30):
                self.assertFalse(pacer.observe("invalid", pacer.generation))
            clock.now = pacer.cooldown_until
            for _ in range(30):
                self.assertFalse(pacer.observe("invalid", generation))
            self.assertEqual(pacer.interval, 16)
            for _ in range(300):
                pacer.observe("invalid", pacer.generation)
            self.assertEqual(pacer.interval, 10)
            self.assertEqual(pacer.defer_rate_limit(None), 300)
            self.assertEqual(pacer.interval, 20)
            self.assertEqual(pacer.minimum_interval, 12.5)

    async def test_late_rate_limit_remembers_interval_used_by_its_request(self):
        clock = FakeTime()
        pacer = checker.RequestPacer(10, adaptive=True)
        with patch.object(checker, "time", clock):
            for _ in range(300):
                pacer.observe("invalid", pacer.generation)
            self.assertLess(pacer.interval, 4)
            pacer.defer_rate_limit(None, request_interval=10)
            self.assertEqual(pacer.minimum_interval, 12.5)
            self.assertEqual(pacer.interval, 20)
            clock.now = 1
            pacer.defer_rate_limit(None, request_interval=24)
            self.assertEqual(pacer.minimum_interval, 30)
            self.assertEqual(pacer.interval, 30)
            self.assertEqual(pacer.backoff, 300)

    async def test_cooldown_extension_rechecks_workers_already_sleeping(self):
        clock = FakeTime()
        pacer = checker.RequestPacer(2)
        pacer.next_start = 2

        async def sleep(delay):
            if not clock.delays:
                pacer.defer_rate_limit("900")
            await clock.sleep(delay)

        with patch.object(checker, "time", clock), patch.object(checker.asyncio, "sleep", sleep):
            self.assertTrue(await pacer.wait(asyncio.Event()))
        self.assertGreaterEqual(clock.now, 900)
        self.assertEqual(pacer.interval, 10)

    async def test_local_cooldown_stays_fixed_and_never_shortens_server_delay(self):
        clock = FakeTime()
        pacer = checker.RequestPacer(2)
        with patch.object(checker, "time", clock):
            self.assertEqual(pacer.defer_rate_limit(None), 300)
            clock.now = 1
            self.assertEqual(pacer.defer_rate_limit("900"), 900)
            self.assertEqual(pacer.backoff, 300)
            self.assertEqual(pacer.interval, 10)
            clock.now = pacer.cooldown_until
            self.assertEqual(pacer.defer_rate_limit(None), 300)
            self.assertEqual(pacer.interval, 20)
            for _ in range(8):
                clock.now = pacer.cooldown_until
                pacer.defer_rate_limit(None)
            self.assertEqual(pacer.backoff, 300)
            self.assertEqual(pacer.interval, 60)
            clock.now = pacer.cooldown_until
            self.assertEqual(pacer.defer_rate_limit("7200"), 7200)


    async def test_legacy_deadline_is_preserved_but_later_waits_are_fixed(self):
        clock = FakeTime()
        pacer = checker.RequestPacer(2)
        with patch.object(checker, "time", clock):
            self.assertEqual(pacer.restore_cooldown(2200, 1200, 20), 1200)
            self.assertEqual(pacer.backoff, 300)
            clock.now = 1
            self.assertEqual(pacer.defer_rate_limit(None), 1199)
            clock.now = pacer.cooldown_until
            self.assertEqual(pacer.defer_rate_limit(None), 300)
            self.assertEqual(pacer.interval, 40)


class ApiTests(unittest.TestCase):
    def test_exact_boolean_and_matching_code_are_required(self):
        code = URLS[0].rsplit("/", 1)[-1]
        for value, expected in ((True, "valid"), (False, "invalid"),
                                ("true", "unknown"), (1, "unknown"), (None, "unknown")):
            with self.subTest(value=value):
                result = classify_api_response(response(URLS[0], {"code": code, "is_valid": value}),
                                               URLS[0])
                self.assertEqual(result[0], expected)
        self.assertEqual(classify_api_response(
            response(URLS[0], {"code": "different", "is_valid": True}), URLS[0])[0], "unknown")

    def test_null_is_invalid_but_unexpected_payloads_are_unknown(self):
        self.assertEqual(classify_api_response(response(URLS[0]), URLS[0])[0], "invalid")
        for payload in ({}, [], True, "is_valid"):
            self.assertEqual(classify_api_response(response(URLS[0], payload), URLS[0])[0], "unknown")
        self.assertEqual(classify_api_response(
            response(URLS[0], text="<html>Join Claude!</html>"), URLS[0])[0], "unknown")

    def test_status_challenges_and_redirects_never_count_as_valid(self):
        for status, expected in ((401, "blocked"), (403, "blocked"), (404, "unknown"),
                                 (302, "unknown"), (429, "rate_limited"), (503, "error")):
            self.assertEqual(classify_api_response(
                response(URLS[0], status=status), URLS[0])[0], expected)
        for page in (response(URLS[0], text="Verify you are human"),
                     response(URLS[0], headers={"cf-mitigated": "challenge"})):
            self.assertEqual(classify_api_response(page, URLS[0])[0], "blocked")
        page = response(URLS[0], {"code": "EMvVYtsAog", "is_valid": True})
        page.url = "https://claude.ai/login"
        self.assertEqual(classify_api_response(page, URLS[0])[0], "unknown")

    def test_generator_uses_the_observed_seven_byte_format(self):
        for _ in range(100):
            code = run.generate_slug()
            decoded = base64.urlsafe_b64decode(code + "==")
            self.assertEqual(len(code), 10)
            self.assertEqual(len(decoded), 7)
            self.assertIn(code[-1], "AQgw")
            self.assertEqual(base64.urlsafe_b64encode(decoded).decode().rstrip("="), code)


class ScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.output = self.folder / "found.txt"
        self.state = self.folder / "search.sqlite3"
        self.factory = stack.enter_context(patch.object(checker.requests, "AsyncSession"))
        self.session = self.factory.return_value.__aenter__.return_value
        self.session.get = AsyncMock(side_effect=lambda url, **kw: SimpleNamespace(
            url=url, status_code=200, text="null", headers={}))

    async def scan(self, urls=URLS, **kwargs):
        return await checker.run_scan(urls, output=self.output, **kwargs)

    async def test_workers_overlap_but_share_one_request_interval(self):
        active = 0
        maximum = 0
        starts = []

        async def get(url, **kwargs):
            nonlocal active, maximum
            starts.append(time.monotonic())
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.03)
            active -= 1
            return SimpleNamespace(url=url, status_code=200, text="null", headers={})

        self.session.get.side_effect = get
        self.assertEqual(await self.scan(workers=3, interval=0.006), 0)
        self.assertEqual(maximum, 3)
        self.assertTrue(all(b - a >= 0.005 for a, b in zip(starts, starts[1:])))
        self.assertEqual(len(starts), len(URLS))

    async def test_valid_stops_loop_and_is_saved(self):
        self.session.get.return_value = response(URLS[0], {"code": "EMvVYtsAog", "is_valid": True})
        self.session.get.side_effect = None
        self.assertEqual(await self.scan(interval=0.02, stop_on_valid=True, state_path=self.state), 0)
        self.session.get.assert_awaited_once()
        self.assertEqual(self.output.read_text(), URLS[0] + "\n")
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.total_attempts(), 1)

    async def test_supplied_list_collects_all_valid_links(self):
        async def get(url, **kwargs):
            code = url.rsplit("/", 1)[-1]
            return SimpleNamespace(url=url, status_code=200, headers={},
                                   text=json.dumps({"code": code, "is_valid": True}))

        self.session.get.side_effect = get
        self.assertEqual(await self.scan(workers=2, interval=0), 0)
        self.assertEqual(set(self.output.read_text().splitlines()), set(URLS))

    async def test_repeated_runs_do_not_duplicate_saved_links(self):
        self.output.write_text(URLS[0], encoding="utf-8")  # Existing file without a final newline.

        async def get(url, **kwargs):
            return response(BASE + url.rsplit("/", 1)[-1],
                            {"code": url.rsplit("/", 1)[-1], "is_valid": True})

        self.session.get.side_effect = get
        self.assertEqual(await self.scan(URLS[:2], workers=1, interval=0), 0)
        self.assertEqual(self.output.read_text().splitlines(), URLS[:2])
        self.assertIn("2 valid link(s), 1 newly saved", self.stdout.getvalue())
        self.assertEqual(await self.scan(URLS[:2], workers=1, interval=0), 0)
        self.assertEqual(self.output.read_text().splitlines(), URLS[:2])
        self.assertIn("2 valid link(s), 0 newly saved", self.stdout.getvalue())

    async def test_rate_limit_blocks_queued_requests_without_retry(self):
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], status=429, headers={"Retry-After": "60"})
        self.assertEqual(await self.scan(workers=4, interval=0.02), 2)
        self.session.get.assert_awaited_once()
        self.assertFalse(self.output.exists())
        self.assertIn("Retry-After: 60", self.stdout.getvalue())
        self.factory.return_value.__aexit__.assert_awaited_once()

    async def test_wait_mode_retries_same_code_after_repeated_rate_limits(self):
        clock = FakeTime()
        starts = []
        pages = iter([response(URLS[0], status=429, headers={"Retry-After": "900"}),
                      response(URLS[0], status=429), response(URLS[0])])

        async def get(url, **kwargs):
            starts.append((url, clock.now))
            return next(pages)

        self.session.get.side_effect = get
        with patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=2,
                                             wait_on_rate_limit=True, state_path=self.state), 0)
        self.assertEqual(starts, [(api_url(URLS[0]), 0), (api_url(URLS[0]), 900),
                                  (api_url(URLS[0]), 1200)])
        self.assertIn("All new requests paused", self.stdout.getvalue())
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.total_attempts(), 3)
            self.assertEqual(state.load_cooldown(), (2200, 300, 20))

    async def test_all_workers_share_one_cooldown(self):
        clock = FakeTime()
        starts = []

        async def get(url, **kwargs):
            starts.append(clock.now)
            if len(starts) == 1:
                return response(URLS[0], status=429)
            return SimpleNamespace(url=url, status_code=200, text="null", headers={})

        self.session.get.side_effect = get
        with patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            self.assertEqual(await self.scan(workers=4, interval=2, wait_on_rate_limit=True), 0)
        self.assertEqual(len(starts), len(URLS) + 1)
        self.assertEqual(starts[0], 0)
        self.assertTrue(all(start >= 300 for start in starts[1:]))
        self.assertTrue(all(b - a >= 10 for a, b in zip(starts[1:], starts[2:])))

    async def test_adaptive_ramp_cooldown_and_floor_survive_restart(self):
        clock = FakeTime()
        starts = []
        urls = [BASE + f"provided-{i}" for i in range(150)]

        async def get(url, **kwargs):
            starts.append(clock.now)
            if len(starts) == 40:
                return response(BASE + url.rsplit("/", 1)[-1], status=429,
                                headers={"Retry-After": "900"})
            return SimpleNamespace(url=url, status_code=200, text="null", headers={})

        self.session.get.side_effect = get
        with patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            self.assertEqual(await self.scan(urls, workers=4, interval=4,
                                             adaptive=True, state_path=self.state), 0)
            self.assertEqual(len(starts), 151)
            self.assertTrue(any(b - a < 3.99 for a, b in zip(starts[:39], starts[1:39])))
            self.assertGreaterEqual(starts[40] - starts[39], 900 - 1e-6)
            self.assertTrue(all(b - a >= 4.5 - 1e-6
                                for a, b in zip(starts[40:], starts[41:])))
            with contextlib.closing(checker.SearchState(self.state)) as state:
                self.assertAlmostEqual(state.load_adaptive_floor(), 4.5)
            starts.clear()
            self.session.get.side_effect = lambda url, **kw: (
                starts.append(clock.now) or SimpleNamespace(
                    url=url, status_code=200, text="null", headers={}))
            # The caller's faster setting cannot erase the saved minimum interval.
            more_urls = [BASE + f"more-{i}" for i in range(150)]
            self.assertEqual(await self.scan(more_urls, workers=4, interval=2,
                                             adaptive=True, state_path=self.state), 0)
            self.assertTrue(all(b - a >= 4.5 - 1e-6 for a, b in zip(starts, starts[1:])))
        self.assertIn("Adaptive pacing: 30 accepted checks", self.stdout.getvalue())

    async def test_restart_preserves_window_and_current_pace_over_old_slowdown(self):
        clock = FakeTime()
        with contextlib.closing(checker.SearchState(self.state)) as state:
            state.save_cooldown(900, 300, 10)  # Expired legacy cooldown.
            state.save_adaptive_floor(2.5)
        urls = [BASE + f"resume-{i}" for i in range(31)]
        with patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            self.assertEqual(await self.scan(urls[:17], workers=1, interval=2,
                                             adaptive=True, state_path=self.state), 0)
            with contextlib.closing(checker.SearchState(self.state)) as state:
                self.assertEqual(state.load_pacing_progress(), (10, 17))
            self.assertEqual(await self.scan(urls[17:30], workers=1, interval=2,
                                             adaptive=True, state_path=self.state), 0)
            with contextlib.closing(checker.SearchState(self.state)) as state:
                self.assertEqual(state.load_pacing_progress(), (9, 0))
            self.assertEqual(await self.scan(urls[30:], workers=1, interval=2,
                                             adaptive=True, state_path=self.state), 0)
            with contextlib.closing(checker.SearchState(self.state)) as state:
                self.assertEqual(state.load_pacing_progress(), (9, 1))
        self.assertIn("Adaptive progress: 17/30", self.stdout.getvalue())
        self.assertIn("one request start per 9s globally", self.stdout.getvalue())

    async def test_new_cooldown_invalidates_saved_pacing_progress(self):
        clock = FakeTime()
        with contextlib.closing(checker.SearchState(self.state)) as state:
            pacer = checker.RequestPacer(4, adaptive=True)
            pacer.accepted = 29
            state.save_pacing_progress(pacer)
            state.save_cooldown(1300, 300, 8)
            self.assertIsNone(state.load_pacing_progress())
        with patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=2,
                                             adaptive=True, state_path=self.state), 0)
        self.assertGreaterEqual(clock.now, 300)
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.load_pacing_progress(), (8, 1))

    async def test_slower_explicit_interval_resets_saved_window(self):
        with contextlib.closing(checker.SearchState(self.state)) as state:
            pacer = checker.RequestPacer(4, adaptive=True)
            pacer.accepted = 29
            state.save_pacing_progress(pacer)
        self.assertEqual(await self.scan(URLS[:1], workers=1, interval=10,
                                         adaptive=True, state_path=self.state), 0)
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.load_pacing_progress(), (10, 1))

    async def test_adaptive_still_stops_on_access_blocks(self):
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], status=403)
        self.assertEqual(await self.scan(workers=4, adaptive=True), 2)
        self.session.get.assert_awaited_once()

    async def test_restart_cannot_skip_saved_cooldown(self):
        clock = FakeTime()
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], status=429)
        with patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=2,
                                             state_path=self.state), 2)
            self.assertEqual(clock.now, 0)
            self.session.get.return_value = response(URLS[0])
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=2,
                                             state_path=self.state), 0)
        self.assertGreaterEqual(clock.now, 300)
        self.assertIn("Saved rate limit: waiting", self.stdout.getvalue())

    async def test_time_limit_can_expire_during_cooldown(self):
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], status=429)
        self.assertEqual(await self.scan(URLS[:1], hours=0.00001, wait_on_rate_limit=True,
                                         state_path=self.state), 0)
        self.session.get.assert_awaited_once()
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.unfinished(), URLS[:1])
            self.assertGreater(state.load_cooldown()[0], time.time())
        self.factory.return_value.__aexit__.assert_awaited_once()

    async def test_wait_mode_still_stops_on_access_blocks(self):
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], status=403)
        self.assertEqual(await self.scan(workers=4, interval=0.02, wait_on_rate_limit=True), 2)
        self.session.get.assert_awaited_once()

    async def test_unknown_schema_stops_instead_of_searching_blindly(self):
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], {"is_valid": True})
        self.assertEqual(await self.scan(workers=4, interval=0.02), 1)
        self.session.get.assert_awaited_once()
        self.assertFalse(self.output.exists())

    async def test_resume_skips_invalid_and_retries_unfinished_first(self):
        with contextlib.closing(checker.SearchState(self.state)) as state:
            state.record(URLS[0], "invalid", "already checked", attempted=True)
            state.record(URLS[1], "pending", "interrupted", attempted=True)
        self.assertEqual(await self.scan(URLS[:3], workers=1, interval=0,
                                         state_path=self.state), 0)
        self.assertEqual([call.args[0] for call in self.session.get.await_args_list],
                         [api_url(URLS[1]), api_url(URLS[2])])
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.total_attempts(), 4)
            self.assertEqual(state.unfinished(), [])

    async def test_duration_cancels_pending_requests_and_keeps_resume_state(self):
        cancelled = []

        async def get(url, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(url)
                raise

        self.session.get.side_effect = get
        self.assertEqual(await self.scan(workers=2, interval=0, hours=0.00001,
                                         state_path=self.state), 0)
        self.assertEqual(len(cancelled), 2)
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(len(state.unfinished()), 2)
            self.assertEqual(state.total_attempts(), 2)
        self.factory.return_value.__aexit__.assert_awaited_once()

    async def test_transient_failures_retry_same_url_with_backoff(self):
        self.session.get.side_effect = [
            checker.RequestException("temporary timeout"),
            response(URLS[0], status=503),
            response(URLS[0]),
        ]
        with patch.object(checker.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=0,
                                             state_path=self.state), 0)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [2.0, 4.0])
        self.assertEqual([call.args[0] for call in self.session.get.await_args_list],
                         [api_url(URLS[0])] * 3)

    async def test_persistent_failure_stops_after_retry_limit(self):
        self.session.get.side_effect = checker.RequestException("offline")
        with patch.object(checker.asyncio, "sleep", new_callable=AsyncMock):
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=0), 2)
        self.assertEqual(self.session.get.await_count, 3)

    async def test_server_retry_after_respects_delay_and_backoff(self):
        for header, expected in (("120", 120), ("0", 2), ("bad", 2),
                                 (formatdate(1120, usegmt=True), 120)):
            with self.subTest(header=header):
                self.session.get.reset_mock()
                self.session.get.side_effect = [
                    response(URLS[0], status=503, headers={"Retry-After": header}),
                    response(URLS[0]),
                ]
                with patch.object(checker.time, "time", return_value=1000), \
                        patch.object(checker.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    self.assertEqual(await self.scan(URLS[:1], workers=1, interval=0), 0)
                sleep.assert_awaited_once_with(expected)
                self.assertEqual(self.session.get.await_count, 2)

    async def test_network_error_does_not_reuse_previous_retry_after(self):
        self.session.get.side_effect = [
            response(URLS[0], status=503, headers={"Retry-After": "120"}),
            checker.RequestException("connection reset"),
            response(URLS[0]),
        ]
        with patch.object(checker.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.scan(URLS[:1], workers=1, interval=0), 0)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [120, 4])

    async def test_deadline_cancels_server_retry_wait_and_preserves_unfinished(self):
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], status=503,
                                                 headers={"Retry-After": "900"})
        self.assertEqual(await self.scan(URLS[:1], workers=1, interval=0,
                                         hours=0.00001, state_path=self.state), 0)
        self.session.get.assert_awaited_once()
        with contextlib.closing(checker.SearchState(self.state)) as state:
            self.assertEqual(state.unfinished(), URLS[:1])
        self.factory.return_value.__aexit__.assert_awaited_once()

    async def test_duplicates_are_only_requested_once(self):
        self.assertEqual(await self.scan([URLS[0]] * 4, workers=4, interval=0), 0)
        self.session.get.assert_awaited_once()

    async def test_output_failure_cancels_other_requests_and_closes_session(self):
        self.output = self.folder
        self.session.get.side_effect = None
        self.session.get.return_value = response(URLS[0], {"code": "EMvVYtsAog", "is_valid": True})
        with self.assertRaises(OSError):
            await self.scan(interval=0.02)
        self.session.get.assert_awaited_once()
        self.factory.return_value.__aexit__.assert_awaited_once()


class CollectCliTests(unittest.TestCase):
    def test_adaptive_uses_conservative_default_and_automatic_resume_state(self):
        with patch.object(run, "run_scan", new_callable=AsyncMock, return_value=0) as scan:
            self.assertEqual(run.main([URLS[0], "--adaptive"]), 0)
        self.assertEqual(scan.await_args.kwargs["interval"], 2)
        self.assertEqual(scan.await_args.kwargs["state_path"], Path("search.sqlite3"))
        self.assertTrue(scan.await_args.kwargs["adaptive"])

    def test_adaptive_rejects_excessive_starting_rate_and_html_matching(self):
        for args in (["--adaptive", "--interval", "1"],
                     ["--adaptive", "--match-text", "anything"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    run.main([URLS[0], *args])
                self.assertEqual(exc.exception.code, 2)

    def test_wait_flag_reaches_checker_and_resumes_after_cooldown(self):
        clock = FakeTime()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), \
                patch.object(checker.requests, "AsyncSession") as factory, \
                patch.object(checker, "time", clock), \
                patch.object(checker.asyncio, "sleep", clock.sleep):
            session = factory.return_value.__aenter__.return_value
            session.get = AsyncMock(side_effect=[response(URLS[0], status=429), response(URLS[0])])
            self.assertEqual(run.main([URLS[0], "--wait-on-rate-limit", "--state",
                                       str(Path(folder) / "state.sqlite3")]), 0)
            self.assertEqual(session.get.await_count, 2)
            self.assertGreaterEqual(clock.now, 300)

    def test_collect_continues_after_success_but_still_stops_on_rate_limit(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            inputs = Path(folder) / "links.txt"
            inputs.write_text("\n".join(URLS), encoding="utf-8")
            for collect in (False, True):
                with self.subTest(collect=collect), patch.object(checker.requests, "AsyncSession") as factory:
                    session = factory.return_value.__aenter__.return_value
                    session.get = AsyncMock(side_effect=[
                        response(URLS[0], {"code": "EMvVYtsAog", "is_valid": True}),
                        response(URLS[1], {"code": "CIV1gruvBg", "is_valid": True}),
                        response(URLS[2], status=429),
                    ])
                    output = Path(folder) / f"found-{collect}.txt"
                    args = ["--loop", "--file", str(inputs), "--workers", "1", "--interval", "0",
                            "--state", str(Path(folder) / f"state-{collect}.sqlite3"),
                            "--output", str(output)]
                    if collect:
                        args.append("--collect")
                    self.assertEqual(run.main(args), 2 if collect else 0)
                    self.assertEqual(session.get.await_count, 3 if collect else 1)
                    self.assertEqual(output.read_text().splitlines(), URLS[:2 if collect else 1])
                    factory.return_value.__aexit__.assert_awaited_once()

    def test_collect_random_search_still_respects_attempt_limit(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), \
                patch.object(checker.requests, "AsyncSession") as factory:
            session = factory.return_value.__aenter__.return_value
            session.get = AsyncMock(side_effect=lambda url, **kw: response(
                BASE + url.rsplit("/", 1)[-1], {"code": url.rsplit("/", 1)[-1], "is_valid": True}))
            output = Path(folder) / "found.txt"
            self.assertEqual(run.main(["--collect", "--max-attempts", "3", "--interval", "0",
                                       "--output", str(output)]), 0)
            self.assertEqual(session.get.await_count, 3)
            self.assertEqual(len(output.read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
