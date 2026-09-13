import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import distributed as d
from referral_api import api_url

URLS = ["https://claude.ai/referral/" + code for code in ("EMvVYtsAog", "CIV1gruvBg", "DG2BFZM8Qg")]
TOKEN = "test-token-" + "a" * 32


def report(job, worker="one", status=200, body="null", headers=None):
    return {"id": job["id"], "lease": job["lease"], "worker": worker,
            "response": {"url": api_url(job["url"]), "status_code": status,
                         "text": body, "headers": headers or {}}}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "queue.sqlite3"
        self.now = 1000
        self.queue = d.Queue(self.path, now=lambda: self.now)
        self.addCleanup(self.queue.close)
        self.queue.add(URLS)

    def test_duplicate_import_and_validation(self):
        self.assertEqual(self.queue.add([URLS[0], URLS[0] + "?source=shared"]), 0)
        with self.assertRaises(ValueError):
            self.queue.add(["newcode", "https://example.com/"])
        self.assertEqual(self.queue.snapshot()["counts"], {"pending": 3})

    def test_concurrent_claims_have_one_global_pace(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.queue.claim, [str(n) for n in range(8)]))
        self.assertEqual(sum(result["state"] == "job" for result in results), 1)
        self.now += 3
        second = self.queue.claim("next")
        self.assertEqual(second["state"], "job")
        self.assertNotEqual(second["id"], next(r["id"] for r in results if r["state"] == "job"))

    def test_limit_and_one_lease_per_worker(self):
        self.queue.workers = 1
        first = self.queue.claim("one")
        self.now += 3
        self.assertEqual(self.queue.claim("two")["state"], "wait")
        self.queue.workers = 4
        self.assertEqual(self.queue.claim("one")["state"], "wait")
        self.assertEqual(self.queue.claim("two")["state"], "job")
        self.assertEqual(first["url"], URLS[0])

    def test_expired_lease_reassigned_and_late_result_rejected(self):
        first = self.queue.claim("one")
        self.now += d.LEASE_SECONDS
        second = self.queue.claim("two")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["lease"], second["lease"])
        self.assertFalse(self.queue.report(report(first))["accepted"])
        self.assertTrue(self.queue.report(report(second, "two"))["accepted"])

    def test_only_exact_valid_api_payload_is_saved(self):
        first = self.queue.claim("one")
        payload = json.dumps({"code": first["url"].rsplit("/", 1)[-1], "is_valid": True})
        self.assertEqual(self.queue.report(report(first, body=payload))["status"], "valid")
        self.assertEqual(self.queue.results()["valid_links"], URLS[:1])
        self.assertTrue(self.queue.report(report(first, body=payload))["duplicate"])
        self.assertEqual(self.queue.snapshot()["counts"]["valid"], 1)
        self.now += 3
        second = self.queue.claim("one")
        result = self.queue.report(report(second, body="<html>Welcome to Claude</html>"))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.queue.claim("another")["state"], "halted")
        self.assertEqual(self.queue.results()["valid_links"], URLS[:1])

    def test_rate_limit_shared_and_duplicate_does_not_extend_wait(self):
        first = self.queue.claim("one")
        self.now += 3
        second = self.queue.claim("two")
        message = report(first, status=429, headers={"Retry-After": "900"})
        self.queue.report(message)
        self.assertEqual(self.queue.snapshot()["cooldown_seconds"], 900)
        self.assertEqual(self.queue.snapshot()["interval"], 6)
        self.now += 10
        self.assertTrue(self.queue.report(message)["duplicate"])
        self.assertEqual(self.queue.snapshot()["cooldown_seconds"], 890)
        self.queue.report(report(second, "two", status=429))
        self.assertEqual(self.queue.snapshot()["interval"], 6)
        self.assertEqual(self.queue.snapshot()["cooldown_seconds"], 890)
        self.assertEqual(self.queue.claim("three")["state"], "wait")
        self.now += 890
        third = self.queue.claim("three")
        self.queue.report(report(third, "three", status=429))
        self.assertEqual(self.queue.snapshot()["cooldown_seconds"], 300)
        self.assertEqual(self.queue.snapshot()["interval"], 12)

    def test_restart_preserves_progress_cooldown_and_learned_pace(self):
        first = self.queue.claim("one")
        self.queue.report(report(first))
        self.now += 3
        second = self.queue.claim("one")
        self.queue.report(report(second, status=429))
        with contextlib.closing(d.Queue(self.path, interval=2, now=lambda: self.now)) as restarted:
            self.assertEqual(restarted.snapshot()["counts"]["invalid"], 1)
            self.assertEqual(restarted.snapshot()["cooldown_seconds"], 300)
            self.assertEqual(restarted.snapshot()["interval"], 6)
            self.assertEqual(restarted.claim("two")["state"], "wait")

    def test_server_error_retries_are_bounded_and_honor_retry_after(self):
        for attempt in range(3):
            first = self.queue.claim("one")
            self.assertEqual(first["state"], "job")
            self.queue.report(report(first, status=503, headers={"Retry-After": "120"}))
            if attempt < 2:
                self.assertEqual(self.queue.claim("two")["state"], "wait")
                self.now += 120
        self.assertEqual(self.queue.claim("two")["state"], "halted")

    def test_access_block_halts_every_worker(self):
        first = self.queue.claim("one")
        self.queue.report(report(first, status=403))
        self.assertEqual(self.queue.claim("two")["state"], "halted")
        self.assertEqual(self.queue.snapshot()["counts"], {"pending": 3})

    def test_done_when_all_links_checked(self):
        for _ in URLS:
            job = self.queue.claim("one")
            self.queue.report(report(job))
            self.now += 3
        self.assertEqual(self.queue.claim("two")["state"], "done")

    def test_invalid_envelope_does_not_complete_job(self):
        first = self.queue.claim("one")
        message = report(first)
        message["response"]["headers"] = {"Retry-After": 30}
        with self.assertRaises(ValueError):
            self.queue.report(message)
        self.assertEqual(self.queue.snapshot()["counts"], {"leased": 1, "pending": 2})


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.queue = d.Queue(":memory:")
        self.queue.add(URLS[:1])
        self.server = d.make_server(self.queue, TOKEN, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = d.Client(self.base, TOKEN)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.queue.close()

    def test_auth_required_for_reads_and_writes(self):
        for endpoint, body in (("/status", None), ("/results", None), ("/claim", {"worker": "one"})):
            with self.subTest(endpoint=endpoint), self.assertRaises(HTTPError) as exc:
                d.Client(self.base, "wrong").call(endpoint, body)
            self.assertEqual(exc.exception.code, 401)
            exc.exception.close()

    def test_http_end_to_end_claim_report_status_results(self):
        job = self.client.call("/claim", {"worker": "one"})
        result = self.client.call("/report", report(job, body=json.dumps({"code": "EMvVYtsAog", "is_valid": True})))
        self.assertEqual(result["status"], "valid")
        self.assertEqual(self.client.call("/results")["valid_links"], URLS[:1])
        self.assertEqual(self.client.call("/status")["counts"], {"valid": 1})
        self.assertEqual(self.client.call("/claim", {"worker": "two"})["state"], "done")

    def test_worker_uses_strict_endpoint_and_stops_after_list(self):
        with patch.object(d.requests, "Session") as factory, contextlib.redirect_stdout(io.StringIO()):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = Mock(status_code=200, url=api_url(URLS[0]), text="null", headers={})
            self.assertEqual(d.run_worker(self.client), 0)
            session.get.assert_called_once_with(api_url(URLS[0]), timeout=10, allow_redirects=False)
        self.assertEqual(self.queue.snapshot()["counts"], {"invalid": 1})


class WorkerTests(unittest.TestCase):
    def test_delayed_claim_is_not_used_for_validation(self):
        client = Mock()
        client.call.side_effect = [
            {"state": "job", "id": 1, "url": URLS[0], "lease": "abc"},
            {"state": "done"},
        ]
        with patch.object(d.requests, "Session") as factory, \
                patch.object(d.time, "monotonic", side_effect=[0, 56, 56]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.run_worker(client), 0)
            factory.return_value.__enter__.return_value.get.assert_not_called()

    def test_lost_report_ack_retries_report_without_rechecking(self):
        client = Mock()
        client.call.side_effect = [
            {"state": "job", "id": 1, "url": URLS[0], "lease": "abc"},
            URLError("lost acknowledgement"), {"accepted": True, "status": "invalid"},
            {"state": "done"},
        ]
        with patch.object(d.requests, "Session") as factory, patch.object(d.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = Mock(status_code=200, url=api_url(URLS[0]), text="null", headers={})
            self.assertEqual(d.run_worker(client), 0)
            session.get.assert_called_once()
        self.assertEqual(client.call.call_args_list[1], client.call.call_args_list[2])

    def test_rejects_plaintext_remote_coordinator(self):
        for url in ("http://192.168.1.2:8765", "ftp://localhost", "https://user:password@example.com"):
            with self.assertRaises(ValueError):
                d.Client(url, TOKEN)


if __name__ == "__main__":
    unittest.main()
