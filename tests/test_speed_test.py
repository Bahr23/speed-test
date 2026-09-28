import argparse
import gc
import io
import os
import subprocess
import sys
import threading
import unittest
import weakref
from concurrent.futures import CancelledError
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest.mock import Mock, call, patch

import requests

import measurement
import speed_test
from tests.support import BODY, COMPRESSED_BODY, SCRIPT, local_server, run_cli


class DownloadTests(unittest.TestCase):
    def setUp(self):
        # External proxy settings must not affect the local test server.
        self.enterContext(patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}))
        self.body_release = threading.Event()
        self.url, self.requests_seen, self.events = self.enterContext(
            local_server(body_release=self.body_release)
        )
        self.session = self.enterContext(requests.Session())
        self.session.trust_env = False

    def test_failed_run_notifies_observer_without_a_display_context(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                observer = Mock(spec=measurement.RunObserver)
                with self.assertRaises(measurement.AttemptError) as caught:
                    measurement.run_measurement(
                        self.url + "/error", 2, parallel=parallel, observer=observer,
                    )
                observer.fail_attempt.assert_called_once_with(caught.exception.number)
                observer.abort.assert_called_once_with()
                observer.finish_attempt.assert_not_called()
                outcomes = [event for event in observer.mock_calls if event[0] in {"fail_attempt", "abort"}]
                self.assertEqual(outcomes, [
                    call.fail_attempt(caught.exception.number), call.abort(),
                ])

    def test_parallel_failure_releases_responses_without_waiting_for_cyclic_gc(self):
        responses = []
        original_send = requests.Session.send

        def send(session, *args, **kwargs):
            response = original_send(session, *args, **kwargs)
            responses.append(weakref.ref(response))
            return response

        gc_was_enabled = gc.isenabled()
        try:
            gc.disable()
            with patch.object(requests.Session, "send", send):
                code, stdout, stderr = run_cli(self.url + "/empty", "--parallel")
            self.assertEqual((code, stdout), (1, ""))
            self.assertIn("Ошибка запроса", stderr)
            self.assertTrue(responses)
            self.assertTrue(all(response() is None for response in responses))
        finally:
            if gc_was_enabled:
                gc.enable()
            # Also let the fixture close cleanly if the regression assertion fails.
            gc.collect()

    def test_core_can_run_both_modes_without_importing_console_modules(self):
        code = (
            "import sys; import measurement; "
            "stats = measurement.run_measurement(sys.argv[1], 2, parallel=sys.argv[2] == 'yes'); "
            "assert 'rich' not in sys.modules; assert 'console_ui' not in sys.modules; "
            "print(stats.total_bytes)"
        )
        for path, parallel in (("/sequential", "no"), ("/parallel", "yes")):
            with self.subTest(parallel=parallel):
                before = len(self.requests_seen)
                result = subprocess.run(
                    [sys.executable, "-c", code, self.url + path, parallel],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(result.stdout, f"{len(BODY) * 10}\n")
                self.assertEqual(len(self.requests_seen) - before, 10)

    def test_ten_complete_sequential_downloads_reuse_connection(self):
        code, stdout, stderr = run_cli(self.url + "/sequential")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(self.requests_seen), 10)
        self.assertEqual(self.events, ["start", "body"] * 10)
        self.assertEqual(len({port for _, port, _ in self.requests_seen}), 1)
        self.assertTrue(all(encoding == "identity" for _, _, encoding in self.requests_seen))
        progress = [line for line in stdout.splitlines() if line.startswith("Запрос ")]
        self.assertEqual(len(progress), 10)
        for number, line in enumerate(progress, 1):
            self.assertTrue(line.startswith(f"Запрос {number}/10:"))
            self.assertIn(f"{len(BODY)} байт", line)
        self.assertIn(f"Скачано: {len(BODY) * 10} байт", stdout)
        self.assertIn("Среднее время запроса:", stdout)
        self.assertIn("Мбит/с", stdout)
        self.assertIn("МБ/с", stdout)

    def test_parallel_flag_starts_ten_requests_together(self):
        code, stdout, stderr = run_cli(self.url + "/parallel", "--parallel")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(self.requests_seen), 10)
        self.assertEqual(self.events[:10], ["start"] * 10)
        self.assertEqual(self.events.count("barrier passed"), 10)
        self.assertEqual(len({port for _, port, _ in self.requests_seen}), 10)
        lines = [line for line in stdout.splitlines() if line.startswith("Запрос ")]
        self.assertEqual(len(lines), 10)
        self.assertEqual({line.split(":")[0] for line in lines}, {f"Запрос {n}/10" for n in range(1, 11)})
        self.assertTrue(all("МБ/с" in line for line in lines))
        self.assertIn(f"Скачано: {len(BODY) * 10} байт", stdout)
        self.assertNotIn("\x1b", stdout)
        # The barrier proves overlap; exact timing is checked with a controlled clock.
        self.assertRegex(stdout, r"Скорость: [\d.]+ МБ/с \([\d.]+ Мбит/с\)")

    def test_parallel_failure_cancels_remaining_attempts_without_summary(self):
        code, stdout, stderr = run_cli(self.url + "/parallel-fail", "--parallel")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests_seen), 10)
        self.assertRegex(stderr, r"Ошибка запроса (?:[1-9]|10)/10:")
        self.assertIn("500", stderr)
        self.assertNotIn("Среднее время", stdout)
        self.assertNotIn("Скорость:", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_parallel_body_errors_and_timeouts_omit_summary(self):
        cases = (
            ("/empty", ()), ("/truncated", ()), ("/broken-chunked", ()),
            ("/slow-headers", ("--timeout", "0.05")),
            ("/slow-body", ("--timeout", "0.05")),
        )
        for path, args in cases:
            with self.subTest(path=path):
                before = len(self.requests_seen)
                code, stdout, stderr = run_cli(self.url + path, "--parallel", *args)
                self.assertEqual((code, stdout), (1, ""))
                self.assertRegex(stderr, r"Ошибка запроса (?:[1-9]|10)/10:")
                self.assertEqual(len(stderr.splitlines()), 1)
                self.assertNotIn("Traceback", stderr)
                # Cancellation may stop some workers before they make a request.
                self.assertTrue(1 <= len(self.requests_seen) - before <= 10)

    def test_download_counts_actual_bytes_without_content_length(self):
        for path in ("/data", "/no-length", "/chunked"):
            with self.subTest(path=path):
                result = measurement.download(self.session, self.url + path, 2)
                self.assertEqual(result.size_bytes, len(BODY))
                self.assertGreater(result.elapsed_seconds, 0)

    def test_compressed_response_counts_compressed_bytes(self):
        result = measurement.download(self.session, self.url + "/gzip", 2)
        self.assertEqual(result.size_bytes, len(COMPRESSED_BODY))
        self.assertLess(result.size_bytes, len(BODY))

    def test_chunked_gzip_counts_wire_body_without_decoding_or_chunk_framing(self):
        updates = []
        result = measurement.download(
            self.session, self.url + "/gzip-chunked", 2,
            on_progress=lambda received, total: updates.append((received, total)),
        )
        self.assertEqual(result.size_bytes, len(COMPRESSED_BODY))
        self.assertEqual(updates[0], (0, None))
        self.assertEqual(updates[-1], (len(COMPRESSED_BODY), None))
        self.assertTrue(all(total is None for _, total in updates))

    def test_cancellation_before_download_makes_no_request(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CancelledError):
            measurement.download(self.session, self.url + "/data", 2, cancel_event=stop)
        self.assertEqual(self.requests_seen, [])

    def test_response_is_closed_on_success_failure_and_cancellation(self):
        original_get = self.session.get
        cases = (
            ("/data", None), ("/error", measurement.DownloadError),
            ("/truncated", measurement.DownloadError), ("/data", CancelledError),
        )
        for path, error in cases:
            with self.subTest(path=path, error=error):
                responses = []
                stop = threading.Event()

                def get_response(*args, **kwargs):
                    response = original_get(*args, **kwargs)
                    responses.append(response)
                    return response

                def on_progress(received, total):
                    if error is CancelledError:
                        stop.set()  # Cancel immediately after the headers arrive.

                expected = nullcontext() if error is None else self.assertRaises(error)
                with patch.object(self.session, "get", side_effect=get_response), expected:
                    measurement.download(
                        self.session, self.url + path, 2,
                        cancel_event=stop, on_progress=on_progress,
                    )
                self.assertEqual(len(responses), 1)
                self.assertTrue(responses[0].raw.closed)
                if error is CancelledError:
                    self.assertEqual(responses[0].raw.tell(), 0)

    def test_small_blocks_report_progress_without_waiting_to_fill_the_buffer(self):
        updates = []

        def on_progress(received, total):
            if received and len(updates) == 1:
                self.assertNotIn("body", self.events)
                self.body_release.set()
            updates.append((received, total))

        try:
            result = measurement.download(
                self.session, self.url + "/small-blocks", 2, on_progress=on_progress,
            )
        finally:
            self.body_release.set()
        self.assertEqual(updates[1], (1024, len(BODY)))
        self.assertEqual(result.size_bytes, len(BODY))

    def test_cancellation_after_partial_progress_does_not_wait_for_the_remaining_body(self):
        stop = threading.Event()

        def on_progress(received, total):
            if received:
                stop.set()

        try:
            with self.assertRaises(CancelledError):
                measurement.download(
                    self.session, self.url + "/small-blocks", 2,
                    on_progress=on_progress, cancel_event=stop,
                )
            self.assertNotIn("body", self.events)
        finally:
            self.body_release.set()

    def test_progress_arrives_before_the_download_finishes(self):
        updates = []

        def on_progress(received, total):
            updates.append((received, total))
            if received == measurement.CHUNK_SIZE:
                self.assertNotIn("body", self.events)

        result = measurement.download(self.session, self.url + "/progress", 2, on_progress=on_progress)
        self.assertEqual(updates[0], (0, len(BODY)))
        self.assertEqual(updates[-1], (result.size_bytes, len(BODY)))
        self.assertTrue(any(0 < received < len(BODY) for received, _ in updates))
        self.assertTrue(all(
            0 < after[0] - before[0] <= measurement.CHUNK_SIZE
            for before, after in zip(updates, updates[1:])
        ))

    def test_progress_uses_unknown_total_without_usable_content_length(self):
        for path in ("/no-length", "/chunked", "/chunked-length", "/invalid-length"):
            with self.subTest(path=path):
                updates = []
                result = measurement.download(
                    self.session, self.url + path, 2,
                    on_progress=lambda received, total: updates.append((received, total)),
                )
                self.assertEqual(updates[0], (0, None))
                self.assertEqual(updates[-1], (len(BODY), None))
                self.assertEqual(result.size_bytes, len(BODY))
                self.assertTrue(all(total is None for _, total in updates))

    def test_compressed_progress_matches_the_transferred_size(self):
        updates = []
        measurement.download(
            self.session, self.url + "/gzip", 2,
            on_progress=lambda received, total: updates.append((received, total)),
        )
        self.assertEqual(updates[0], (0, len(COMPRESSED_BODY)))
        self.assertEqual(updates[-1], (len(COMPRESSED_BODY), len(COMPRESSED_BODY)))

    def test_redirects_are_followed_for_each_measurement(self):
        code, stdout, stderr = run_cli(self.url + "/redirect")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual([path for path, _, _ in self.requests_seen], ["/redirect", "/data"] * 10)
        self.assertIn(f"Скачано: {len(BODY) * 10} байт", stdout)

    def test_redirect_to_http_error_stops_the_first_attempt(self):
        code, stdout, stderr = run_cli(self.url + "/redirect-error")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("Ошибка запроса 1/10:", stderr)
        self.assertIn("500", stderr)
        self.assertEqual([path for path, _, _ in self.requests_seen], ["/redirect-error", "/error"])

    def test_redirect_loop_stops_at_the_session_limit_without_summary(self):
        self.session.max_redirects = 3
        with patch("measurement.requests.Session", return_value=self.session):
            code, stdout, stderr = run_cli(self.url + "/redirect-loop")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("Ошибка запроса 1/10:", stderr)
        self.assertIn("redirects", stderr.lower())
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(len(self.requests_seen), 4)  # Initial GET plus three redirects.

    def test_duration_includes_waiting_for_response_body(self):
        result = measurement.download(self.session, self.url + "/delayed", 2)
        self.assertEqual(result.size_bytes, len(BODY))
        self.assertGreaterEqual(result.elapsed_seconds, 0.08)

    def test_continuous_data_can_take_longer_than_the_idle_timeout(self):
        timeout = 0.25
        result = measurement.download(self.session, self.url + "/steady", timeout)
        self.assertEqual(result.size_bytes, len(BODY))
        self.assertGreater(result.elapsed_seconds, timeout)

    def test_http_errors_empty_and_truncated_bodies_stop_without_retry(self):
        for path in ("/error", "/not-found", "/empty", "/truncated", "/broken-chunked"):
            with self.subTest(path=path):
                before = len(self.requests_seen)
                code, stdout, stderr = run_cli(self.url + path)
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests_seen) - before, 1)
                self.assertEqual(stdout, "")
                self.assertIn("Ошибка запроса 1/10:", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_later_error_preserves_progress_but_omits_summary(self):
        code, stdout, stderr = run_cli(self.url + "/fail-third")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests_seen), 3)
        self.assertIn("Запрос 2/10:", stdout)
        self.assertNotIn("Запрос 3/10:", stdout)
        self.assertNotIn("Скорость:", stdout)
        self.assertNotIn("Среднее время", stdout)
        self.assertIn("Ошибка запроса 3/10:", stderr)
        self.assertIn("500", stderr)

    def test_header_and_body_timeouts_stop_without_summary(self):
        for path in ("/slow-headers", "/slow-body"):
            with self.subTest(path=path):
                before = len(self.requests_seen)
                code, stdout, stderr = run_cli(self.url + path, "--timeout", "0.05")
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests_seen) - before, 1)
                self.assertEqual(stdout, "")
                self.assertIn("Ошибка запроса 1/10:", stderr)
                self.assertIn("timed out", stderr.lower())
                self.assertNotIn("Traceback", stderr)

    def test_script_entrypoint(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), self.url + "/data"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(self.requests_seen), 10)
        self.assertIn("Скорость:", result.stdout)
        self.assertNotIn("\x1b", result.stdout)


class CalculationTests(unittest.TestCase):
    def test_parallel_speed_uses_wall_time_but_average_uses_request_times(self):
        measurements = [measurement.Measurement(1_000_000, 2)] * 10
        stats = measurement.calculate_statistics(measurements, wall_seconds=2.5)
        self.assertEqual(stats.average_seconds, 2)
        self.assertEqual(stats.total_bytes, 10_000_000)
        self.assertEqual(stats.megabytes_per_second, 4)
        self.assertEqual(stats.megabits_per_second, 32)

    def test_statistics_use_totals_not_average_of_individual_speeds(self):
        measurements = [
            measurement.Measurement(1_000_000, 1.0),
            measurement.Measurement(9_000_000, 3.0),
        ] * 5
        stats = measurement.calculate_statistics(measurements)
        self.assertEqual(stats.total_bytes, 50_000_000)
        self.assertEqual(stats.average_seconds, 2.0)
        self.assertEqual(stats.megabytes_per_second, 2.5)
        self.assertEqual(stats.megabits_per_second, 20.0)

    def test_calculation_does_not_round_intermediate_values(self):
        stats = measurement.calculate_statistics([measurement.Measurement(1_000_001, 3.0)] * 10)
        self.assertEqual(stats.megabytes_per_second, 10_000_010 / 30 / 1_000_000)

    def test_missing_or_zero_duration_measurements_are_rejected(self):
        for measurements in ([], [measurement.Measurement(100, 0)]):
            with self.subTest(measurements=measurements), self.assertRaises(ValueError):
                measurement.calculate_statistics(measurements)

    def test_invalid_wall_times_are_rejected(self):
        measurements = [measurement.Measurement(1_000_000, 1)] * 10
        for elapsed in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                measurement.calculate_statistics(measurements, wall_seconds=elapsed)


class ArgumentAndErrorTests(unittest.TestCase):
    def test_bad_arguments_exit_with_code_two_before_requesting(self):
        cases = [
            [],
            ["example.com/file"],
            ["ftp://example.com/file"],
            ["https://"],
            ["http://[broken"],
            ["http://example.com:invalid"],
            ["http://example.com:65536"],
            ["http://bad host/file"],
            ["https://example.com", "--timeout"],
            ["https://example.com", "--unknown"],
            ["https://example.com", "--parallel", "2"],
        ] + [["https://example.com", "--timeout", value] for value in (
            "0", "-1", "nan", "inf", "1e309", "1e100", "invalid"
        )]
        with patch("measurement.requests.Session") as session:
            for args in cases:
                with self.subTest(args=args), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        speed_test.main(args)
                    self.assertEqual(caught.exception.code, 2)
            session.assert_not_called()

    def test_http_https_and_positive_fractional_timeout_are_accepted(self):
        for url in ("http://localhost:8000/file?a=1&b=2", "https://example.com/file"):
            self.assertEqual(speed_test.http_url(url), url)
        self.assertEqual(speed_test.positive_timeout("0.5"), 0.5)

    def test_timeout_platform_limit_is_validated(self):
        self.assertEqual(speed_test.positive_timeout(str(speed_test.MAX_TIMEOUT)), speed_test.MAX_TIMEOUT)
        with self.assertRaises(argparse.ArgumentTypeError):
            speed_test.positive_timeout(str(speed_test.MAX_TIMEOUT + 1))

    def test_help_explains_timeout(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            speed_test.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(
            "не ограничивает общую длительность скачивания",
            " ".join(output.getvalue().split()),
        )

    def test_connection_failure_is_reported_without_traceback(self):
        with patch.object(requests.Session, "get", side_effect=requests.ConnectionError("offline")):
            code, stdout, stderr = run_cli("https://example.com/file")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("Ошибка запроса 1/10: offline", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_keyboard_interrupt_returns_130_without_traceback(self):
        with patch.object(requests.Session, "get", side_effect=KeyboardInterrupt):
            code, stdout, stderr = run_cli("https://example.com/file")
        self.assertEqual((code, stdout), (130, ""))
        self.assertIn("Замер прерван", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
