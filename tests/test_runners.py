"""Check orchestration timing without depending on CPU or network speed."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import measurement
from measurement import RunObserver


class RunnerTimingTests(unittest.TestCase):
    def test_keyboard_interrupt_aborts_observer_for_both_execution_modes(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                observer = Mock(spec=RunObserver)
                with patch("measurement.download", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        measurement.run_measurement(
                            "https://example.invalid/file", 30,
                            parallel=parallel, observer=observer,
                        )
                observer.abort.assert_called_once_with()
                observer.fail_attempt.assert_not_called()
                observer.finish_attempt.assert_not_called()

    def test_sequential_speed_excludes_time_spent_printing_results(self):
        clock = SimpleNamespace(now=100.0)
        display = Mock(spec=RunObserver)

        def download(*args, **kwargs):
            clock.now += 2
            return measurement.Measurement(1_000_000, 2)

        def slow_output(*args):
            clock.now += 1000

        display.finish_attempt.side_effect = slow_output
        with (
            patch("measurement.download", side_effect=download),
            patch("measurement.time", SimpleNamespace(perf_counter=lambda: clock.now)),
        ):
            stats = measurement.run_measurement("https://example.invalid/file", 30, observer=display)

        self.assertEqual(display.finish_attempt.call_count, 10)
        self.assertEqual(stats, measurement.Statistics(10_000_000, 2, 0.5, 4))

    def test_parallel_speed_ends_at_last_download_not_at_last_print(self):
        arrivals = threading.Barrier(10)
        caller = threading.get_ident()
        output_started = threading.Event()
        display = Mock(spec=RunObserver)

        def clock():
            if threading.get_ident() != caller:
                return 102.0  # All downloads finish two seconds after group launch.
            return 1102.0 if output_started.is_set() else 100.0

        def download(*args, **kwargs):
            arrivals.wait(timeout=3)
            return measurement.Measurement(1_000_000, 2)

        display.finish_attempt.side_effect = lambda *args: output_started.set()
        with (
            patch("measurement.download", side_effect=download),
            patch("measurement.time", SimpleNamespace(perf_counter=clock)),
        ):
            stats = measurement.run_measurement(
                "https://example.invalid/file", 30, parallel=True, observer=display,
            )

        self.assertEqual(display.finish_attempt.call_count, 10)
        self.assertEqual(stats, measurement.Statistics(10_000_000, 2, 5, 40))


if __name__ == "__main__":
    unittest.main()
