import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run

LINK = run.BASE_URL + "Abc123-_xy"


def response(status=200, text="", url=LINK, headers=None):
    return SimpleNamespace(status_code=status, text=text, url=url, headers=headers or {})


class ResponseTests(unittest.TestCase):
    def test_generic_success_and_redirects_are_unverified(self):
        for page in (
            response(text="<h1>Welcome to Claude</h1>"),
            response(text="<div id='app'></div>"),
            response(text="Claim offer", url="https://claude.ai/login"),
            response(text="Claim offer", url=run.BASE_URL + "different"),
        ):
            with self.subTest(page=page):
                self.assertEqual(run.classify_response(page, LINK)[0], "unknown")

    def test_invalid_text_is_normalized_and_scripts_are_ignored(self):
        page = response(text="<p>This REFERRAL link&nbsp;is <b>no longer</b> valid</p>")
        self.assertEqual(run.classify_response(page, LINK)[0], "invalid")
        page = response(text="""
            <script>const error = "Page not found"; const id = "404";</script>
            <style>.invalid::after { content: "invalid referral link"; }</style>
            <template><p>Referral link has expired</p></template>
            <img src="/asset-404.png"><h1>Welcome</h1>
        """)
        self.assertEqual(run.classify_response(page, LINK)[0], "unknown")

    def test_status_codes_and_challenges(self):
        for status, expected in ((401, "blocked"), (403, "blocked"),
                                 (404, "invalid"), (410, "invalid"),
                                 (429, "rate_limited"), (500, "error")):
            with self.subTest(status=status):
                self.assertEqual(run.classify_response(response(status), LINK)[0], expected)
        for page in (response(text="<title>Just a moment...</title>"),
                     response(text="Verify you are human", url="https://claude.ai/challenge"),
                     response(headers={"cf-mitigated": "challenge"})):
            self.assertEqual(run.classify_response(page, LINK)[0], "blocked")
        result = run.classify_response(response(429, headers={"Retry-After": "60"}), LINK)
        self.assertIn("60", result[1])

    def test_candidates_require_text_and_invalid_takes_priority(self):
        page = response(text="<p>Claim your <strong>seven day</strong> offer</p>")
        self.assertEqual(run.classify_response(page, LINK)[0], "unknown")
        self.assertEqual(run.classify_response(page, LINK, "CLAIM  YOUR seven day offer")[0],
                         "candidate")
        page.text += "<p>This referral link is no longer valid</p>"
        self.assertEqual(run.classify_response(page, LINK, "claim your")[0], "invalid")


class CommandTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.session_factory = stack.enter_context(patch.object(run.requests, "Session"))
        self.session = self.session_factory.return_value.__enter__.return_value
        self.session.get.return_value = response(404)
        self.sleep = stack.enter_context(patch.object(run.time, "sleep"))
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.output = self.directory / "candidates.txt"

    def test_random_search_respects_attempt_limit(self):
        self.session.get.side_effect = lambda url, **kwargs: response(text="null", url=url)
        self.assertEqual(run.main(["--max-attempts", "3", "--interval", "0"]), 0)
        self.assertEqual(self.session.get.call_count, 3)
        self.session_factory.assert_called_once_with(impersonate="chrome")

    def test_file_and_positional_links_are_deduplicated(self):
        self.session.get.side_effect = lambda url, **kwargs: response(text="null", url=url)
        inputs = self.directory / "links.txt"
        inputs.write_text("# comment\n\n" + LINK + "/?tracking=1\nOtherCode\n", encoding="utf-8")
        result = run.main(["Abc123-_xy", "--file", str(inputs), "--interval", "0"])
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in self.session.get.call_args_list],
                         [run.api_url(LINK), run.api_url(run.BASE_URL + "OtherCode")])

    def test_request_duration_counts_toward_interval(self):
        now = [0.0]
        starts = []

        def get(url, **kwargs):
            starts.append(now[0])
            now[0] += 0.75
            return response(404, url=url)

        self.session.get.side_effect = get
        self.sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        with patch.object(run.time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(run.run_checks([LINK] * 3, interval=2), 0)
        self.assertEqual(starts, [0.0, 2.0, 4.0])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.25, 1.25])
        self.assertEqual(now[0], 4.75)  # No sleep after the final response.

    def test_slow_requests_do_not_add_unnecessary_sleep(self):
        now = [0.0]

        def get(*args, **kwargs):
            now[0] += 3.0
            return response(404)

        self.session.get.side_effect = get
        with patch.object(run.time, "monotonic", side_effect=lambda: now[0]):
            run.run_checks([LINK] * 2, interval=2)
        self.sleep.assert_not_called()

    def test_blocked_or_error_response_stops_further_requests(self):
        for page in (response(401), response(403), response(429), response(503),
                     response(text="<h1>Verify you are human</h1>")):
            with self.subTest(page=page):
                self.session.get.reset_mock()
                self.session.get.return_value = page
                self.assertEqual(run.run_checks([LINK] * 3, interval=0), 2)
                self.session.get.assert_called_once()
        self.session_factory.return_value.__exit__.assert_called()

    def test_unknown_random_response_is_not_saved_as_working(self):
        self.session.get.return_value = response(text="<h1>Welcome to Claude</h1>")
        result = run.run_checks([LINK] * 3, random_search=True, output=self.output)
        self.assertEqual(result, 1)
        self.session.get.assert_called_once()
        self.assertFalse(self.output.exists())
        self.assertNotIn("WORKING LINK FOUND", self.stdout.getvalue())
        self.sleep.assert_not_called()

    def test_known_unknown_links_can_all_be_checked(self):
        self.session.get.return_value = response()
        self.assertEqual(run.run_checks([LINK] * 2, interval=0), 0)
        self.assertEqual(self.session.get.call_count, 2)

    def test_matching_candidate_is_saved_and_stops(self):
        self.session.get.return_value = response(text="<p>Claim your seven day offer</p>")
        result = run.run_checks([LINK] * 3, match_text="claim your seven day offer",
                                output=self.output)
        self.assertEqual(result, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), LINK + "\n")
        self.session.get.assert_called_once()
        self.assertIn("unverified candidate", self.stdout.getvalue())
        self.sleep.assert_not_called()

    def test_network_failure_is_reported_and_session_closed(self):
        self.session.get.side_effect = run.RequestException("timeout")
        self.assertEqual(run.main(["Abc123-_xy", "--timeout", "3"]), 2)
        self.assertIn("Network error", self.stderr.getvalue())
        self.session.get.assert_called_once_with(run.api_url(LINK), timeout=3.0, allow_redirects=False)
        self.session_factory.return_value.__exit__.assert_called_once()

    def test_output_error_is_not_reported_as_network_failure(self):
        self.session.get.return_value = response(text="<p>Claim offer</p>")
        result = run.main(["Abc123-_xy", "--match-text", "Claim offer",
                           "--output", str(self.directory)])
        self.assertEqual(result, 2)
        self.assertIn("File or system error", self.stderr.getvalue())
        self.assertNotIn("Network error", self.stderr.getvalue())

    def test_ctrl_c_closes_session(self):
        self.session.get.side_effect = KeyboardInterrupt
        self.assertEqual(run.main(["Abc123-_xy"]), 130)
        self.session_factory.return_value.__exit__.assert_called_once()

    def test_invalid_arguments_never_start_requests(self):
        empty = self.directory / "empty.txt"
        empty.write_text("# no links\n", encoding="utf-8")
        for args in (
            ["--max-attempts", "0"], ["--max-attempts", "-1"],
            ["--interval", "-1"], ["--interval", "nan"], ["--timeout", "inf"],
            ["--timeout", "0"], ["--match-text", "  "],
            ["--workers", "17"], ["--hours", "0"], ["--loop", "--max-attempts", "1"],
            ["--match-text", "anything", "--loop"],
            ["--match-text", "anything", "--collect"],
            ["--match-text", "anything", "--wait-on-rate-limit"],
            ["--file", str(empty)], ["--file", str(self.directory / "missing")],
            ["https://example.com/referral/code"], ["https://claude.ai/login"],
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit) as exc:
                run.main(args)
            self.assertEqual(exc.exception.code, 2)
        self.session_factory.assert_not_called()


class ImportTests(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        result = subprocess.run([sys.executable, "-c", "import run"],
                                cwd=Path(run.__file__).parent,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
