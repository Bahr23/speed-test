import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from rich.console import Console

from console_ui import RunDisplay
from measurement import Measurement, calculate_statistics


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"TERM": "xterm-256color"}))

    def make_display(self, width=80, **kwargs):
        output = io.StringIO()
        console = Console(file=output, width=width, force_terminal=True, color_system=None)
        return RunDisplay(10, console=console, **kwargs), console, output

    def snapshot(self, display, console, output):
        output.seek(0)
        output.truncate()
        console.print(display.render())
        return output.getvalue()

    def test_initial_table_shows_all_ten_waiting_attempts(self):
        display, console, output = self.make_display()
        rendered = self.snapshot(display, console, output)
        self.assertIn("Завершено: 0/10", rendered)
        self.assertEqual(rendered.count("Ожидание"), 10)
        self.assertNotIn("Готово", rendered)
        self.assertNotIn("%", rendered)

    def test_running_speed_decreases_during_a_pause_in_data(self):
        display, console, output = self.make_display()
        with patch("console_ui.time.perf_counter", return_value=100):
            display.start_attempt(1)
        display.update(1, 2_000_000, 4_000_000)
        with patch("console_ui.time.perf_counter", return_value=102):
            first = self.snapshot(display, console, output)
        with patch("console_ui.time.perf_counter", return_value=104):
            later = self.snapshot(display, console, output)
        self.assertIn("1.000", first)
        self.assertIn("0.500", later)
        self.assertNotIn("1.000", later)
        self.assertIn("4.00 с", later)
        self.assertIn("50%", later)

    def test_late_callbacks_cannot_restart_finished_cancelled_or_skipped_rows(self):
        display, console, output = self.make_display(parallel=True)
        with patch("console_ui.time.perf_counter", return_value=100):
            display.start_attempt(1)
            display.start_attempt(2)
        display.update(1, 1_000_000, 1_000_000)
        display.finish_attempt(1, Measurement(1_000_000, 2))
        display.update(2, 1_000_000, 2_000_000)
        with patch("console_ui.time.perf_counter", return_value=103):
            display.abort()
            before = self.snapshot(display, console, output)
        with patch("console_ui.time.perf_counter", return_value=500):
            for number in (1, 2, 3):
                display.start_attempt(number)
                display.update(number, 9_000_000, 9_000_000)
                display.finish_attempt(number, Measurement(9_000_000, 1))
                display.fail_attempt(number)
            display.abort()
            after = self.snapshot(display, console, output)
        self.assertEqual(after, before)
        self.assertEqual(after.count("Готово"), 1)
        self.assertEqual(after.count("Прервано"), 1)
        self.assertEqual(after.count("Не запускалась"), 8)

    def test_finished_unknown_length_shows_actual_volume_without_percentage(self):
        display, console, output = self.make_display()
        display.start_attempt(1)
        display.update(1, 2_000_000, None)
        display.finish_attempt(1, Measurement(2_000_000, 0.5))
        rendered = self.snapshot(display, console, output)
        self.assertIn("Завершено: 1/10", rendered)
        self.assertIn("Готово", rendered)
        self.assertIn("2.0 МБ", rendered)
        self.assertIn("4.000", rendered)
        self.assertNotIn("%", rendered)

    def test_table_preserves_status_volume_and_time_at_supported_widths(self):
        for width in (60, 80, 120):
            with self.subTest(width=width):
                display, console, output = self.make_display(width)
                display.start_attempt(1)
                display.update(1, 5_000_000, 5_000_000)
                display.finish_attempt(1, Measurement(5_000_000, 0.5))
                display.start_attempt(2)
                display.update(2, 3_200_000, 8_000_000)
                display.attempts[1].started -= 0.32
                rendered = self.snapshot(display, console, output)
                self.assertIn("Завершено: 1/10", rendered)
                self.assertIn("Готово", rendered)
                self.assertIn("0.50 с", rendered)
                self.assertIn("Загрузка", rendered)
                self.assertIn("3.2 / 8.0 МБ", rendered)
                self.assertIn("40%", rendered)
                self.assertIn("МБ/с", rendered)
                self.assertIn("10.000", rendered)
                self.assertEqual(rendered.count("Ожидание"), 8)
                self.assertTrue(all(len(line) <= width for line in rendered.splitlines()))
                if width < 80:
                    self.assertNotIn("━", rendered)
                else:
                    self.assertIn("━", rendered)

    def test_waiting_for_response_then_unknown_length_shows_no_percentage(self):
        display, console, output = self.make_display()
        display.start_attempt(1)
        connecting = self.snapshot(display, console, output)
        self.assertIn("Ожидание ответа", connecting)
        self.assertNotIn("%", connecting)
        display.update(1, 3_200_000, None)
        loading = self.snapshot(display, console, output)
        self.assertIn("Загрузка", loading)
        self.assertIn("3.2 МБ", loading)
        self.assertNotIn("%", loading)

    def test_long_download_durations_are_readable_at_all_supported_widths(self):
        for width in (60, 80, 120):
            for elapsed in (999.99, 1234.5, 86400):
                with self.subTest(width=width, elapsed=elapsed):
                    display, console, output = self.make_display(width)
                    display.start_attempt(1)
                    display.update(1, 123_000_000, 123_000_000)
                    display.finish_attempt(1, Measurement(123_000_000, elapsed))
                    rendered = self.snapshot(display, console, output)
                    self.assertNotIn("…", rendered)
                    self.assertIn("123.0 / 123.0 МБ", rendered)
                    self.assertIn("Готово", rendered)
                    self.assertTrue(all(len(line) <= width for line in rendered.splitlines()))

    def test_full_byte_count_does_not_mark_an_unfinished_download_successful(self):
        display, console, output = self.make_display()
        display.start_attempt(1)
        display.update(1, 100, 100)
        loading = self.snapshot(display, console, output)
        self.assertNotIn("100%", loading)
        self.assertNotIn("Готово", loading)
        display.fail_attempt(1)
        display.abort()
        failed = self.snapshot(display, console, output)
        self.assertIn("Ошибка", failed)
        self.assertIn("Завершено: 0/10", failed)
        self.assertEqual(failed.count("Не запускалась"), 9)
        self.assertNotIn("100%", failed)

    def test_completed_and_interrupted_rows_keep_their_results(self):
        display, console, output = self.make_display()
        display.start_attempt(1)
        display.update(1, 5_000_000, 5_000_000)
        display.finish_attempt(1, Measurement(5_000_000, 0.5))
        display.start_attempt(2)
        display.update(2, 3_200_000, 8_000_000)
        display.abort()
        rendered = self.snapshot(display, console, output)
        self.assertIn("Прервано", rendered)
        self.assertIn("3.2 / 8.0 МБ", rendered)
        self.assertIn("0.50 с", rendered)
        self.assertEqual(rendered.count("Не запускалась"), 8)
        self.assertEqual(rendered.count("Готово"), 1)
        self.assertIsNotNone(display.attempts[1].elapsed)

    def test_parallel_rows_and_speeds_are_independent(self):
        display, console, output = self.make_display(120, parallel=True)
        with patch("console_ui.time.perf_counter", return_value=100.0):
            display.start_attempt(1)
            display.start_attempt(2)
        display.update(1, 3_000_000, 5_000_000)
        display.update(2, 1_000_000, None)
        with patch("console_ui.time.perf_counter", return_value=102.0):
            rendered = self.snapshot(display, console, output)
        self.assertIn("Параллельно", rendered)
        self.assertEqual(rendered.count("Загрузка"), 2)
        self.assertIn("1.500", rendered)
        self.assertIn("0.500", rendered)
        display.finish_attempt(2, Measurement(1_000_000, 0.5))
        display.fail_attempt(1)
        display.abort()
        # A late worker callback must not revive a stopped attempt.
        display.update(1, 5_000_000, 5_000_000)
        display.finish_attempt(1, Measurement(5_000_000, 1))
        self.assertEqual(display.attempts[0].status, "error")
        self.assertEqual(display.attempts[0].received, 3_000_000)
        self.assertEqual(display.attempts[1].status, "done")

    def test_speed_is_visible_in_plain_progress_lines(self):
        display, _, output = self.make_display(plain=True)
        display.start_attempt(1)
        display.finish_attempt(1, Measurement(5_000_000, 0.5))
        self.assertIn("10.000 МБ/с", output.getvalue())

    def test_plain_progress_and_summary_use_the_supplied_output_without_ansi_or_wrapping(self):
        display, _, output = self.make_display(width=10, plain=True)
        global_stdout = io.StringIO()
        result = Measurement(5_000_000, 0.5)
        with redirect_stdout(global_stdout):
            display.start_attempt(1)
            display.finish_attempt(1, result)
            display.show_summary(calculate_statistics([result] * 10))
        self.assertEqual(global_stdout.getvalue(), "")
        self.assertIn("Запрос 1/10: 0.500 с, 5000000 байт, 10.000 МБ/с\n", output.getvalue())
        self.assertIn("Скорость: 10.000 МБ/с (80.000 Мбит/с)\n", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())

    def test_summary_uses_the_calculated_statistics(self):
        display, _, output = self.make_display()
        stats = calculate_statistics([Measurement(5_000_000, 0.5)] * 10)
        display.show_summary(stats)
        self.assertIn("80.000 Мбит/с", output.getvalue())
        self.assertIn("10.000 МБ/с", output.getvalue())
        self.assertIn("50000000 байт", output.getvalue())

    def test_plain_redirected_and_dumb_output_disable_live_display(self):
        display, _, _ = self.make_display(plain=True)
        self.assertFalse(display.enabled)
        self.assertIsNone(display.live)
        redirected = RunDisplay(10, console=Console(file=io.StringIO(), force_terminal=False))
        self.assertFalse(redirected.enabled)
        with patch.dict(os.environ, {"TERM": "dumb"}):
            dumb, _, _ = self.make_display()
        self.assertFalse(dumb.enabled)
        self.assertIsNone(dumb.live)


if __name__ == "__main__":
    unittest.main()
