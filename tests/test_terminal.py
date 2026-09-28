"""Exercise real ANSI output and SIGINT, the signal produced by Ctrl+C."""

import os
import re
import signal
import subprocess
import sys
import time
import unittest

from tests.support import SCRIPT, local_server

if os.name == "posix":
    import fcntl
    import pty
    import select
    import struct
    import termios


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@unittest.skipUnless(os.name == "posix", "PTY tests require a POSIX terminal")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.url, self.requests_seen, _ = self.enterContext(local_server())

    def run_terminal(
        self, path, *, width=80, args=(), interrupt_at=None,
        cancel_timeout=2, term="xterm-256color",
    ):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, width, 0, 0))
        env = {
            **os.environ, "TERM": term, "NO_PROXY": "*", "no_proxy": "*",
            "PYTHONIOENCODING": "utf-8", "COLUMNS": str(width), "LINES": "30",
        }
        # Ambient test-runner settings must not turn off animation in the child.
        for key in ("CI", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "NO_COLOR", "FORCE_COLOR"):
            env.pop(key, None)
        process = None
        captured = bytearray()
        interrupted = False
        try:
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), self.url + path, *args],
                stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE, env=env,
            )
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break  # A closed PTY reports EIO on some systems.
                if not chunk:
                    break
                captured.extend(chunk)
                if interrupt_at is not None and not interrupted:
                    text = ANSI.sub("", captured.decode("utf-8", errors="replace"))
                    frames = re.findall(f"([{SPINNERS}]) {interrupt_at}", text)
                    if len(set(frames)) >= 3:
                        process.send_signal(signal.SIGINT)
                        interrupted = True
                        deadline = time.monotonic() + cancel_timeout
            else:
                self.fail("CLI did not finish within its execution/cancellation deadline")
            _, stderr = process.communicate(timeout=2)
            if interrupt_at is not None:
                self.assertTrue(interrupted, "Spinner did not animate during the network wait")
            return process.returncode, captured.decode("utf-8"), stderr.decode("utf-8")
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def assert_cursor_restored(self, stdout):
        self.assertIn("\x1b[?25l", stdout)
        self.assertGreater(stdout.rfind("\x1b[?25h"), stdout.rfind("\x1b[?25l"))

    def test_success_at_three_terminal_widths(self):
        for width in (60, 80, 120):
            with self.subTest(width=width):
                code, stdout, stderr = self.run_terminal("/sequential", width=width)
                text = ANSI.sub("", stdout)
                self.assertEqual((code, stderr), (0, ""))
                self.assertIn("Завершено: 10/10", text)
                self.assertIn("Результат", text)
                self.assertIn("Мбит/с", text)
                self.assertIn("100%", text)
                self.assertNotIn("…", text)
                self.assert_cursor_restored(stdout)

    def test_spinner_animates_during_header_and_body_waits_and_ctrl_c_cleans_up(self):
        for path, phase in (("/ui-wait", "Ожидание ответа"), ("/ui-stream", "Загрузка")):
            with self.subTest(path=path):
                code, stdout, stderr = self.run_terminal(path, interrupt_at=phase)
                text = ANSI.sub("", stdout)
                self.assertEqual(code, 130)
                self.assertIn("Прервано", text)
                self.assertIn("Не запускалась", text)
                self.assertNotIn("Результат", text)
                self.assertIn("Замер прерван", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assert_cursor_restored(stdout)

    def test_error_and_timeout_keep_failed_rows_and_restore_cursor(self):
        for path, args in (("/truncated", ()), ("/ui-wait", ("--timeout", "0.15"))):
            with self.subTest(path=path):
                code, stdout, stderr = self.run_terminal(path, args=args)
                text = ANSI.sub("", stdout)
                self.assertEqual(code, 1)
                self.assertIn("Ошибка", text)
                self.assertIn("Не запускалась", text)
                self.assertNotIn("Готово", text)
                self.assertNotIn("100%", text)
                self.assertNotIn("Результат", text)
                self.assertIn("Ошибка запроса 1/10:", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assert_cursor_restored(stdout)

    def test_parallel_terminal_success_and_failure(self):
        for path, expected in (("/parallel", 0), ("/parallel-fail", 1)):
            with self.subTest(path=path):
                code, stdout, stderr = self.run_terminal(path, args=("--parallel",))
                text = ANSI.sub("", stdout)
                self.assertEqual(code, expected)
                self.assertIn("Параллельно", text)
                self.assertIn("МБ/с", text)
                self.assertNotIn("Traceback", stderr)
                self.assert_cursor_restored(stdout)
                if expected == 0:
                    self.assertEqual(stderr, "")
                    self.assertIn("Завершено: 10/10", text)
                    self.assertIn("Результат", text)
                else:
                    self.assertIn("Ошибка", text)
                    self.assertNotIn("Результат", text)
                    self.assertRegex(stderr, r"Ошибка запроса (?:[1-9]|10)/10:")

    def test_parallel_ctrl_c_stops_active_rows_without_summary(self):
        code, stdout, stderr = self.run_terminal(
            "/ui-stream", args=("--parallel",), interrupt_at="Загрузка",
        )
        text = ANSI.sub("", stdout)
        self.assertEqual(code, 130)
        self.assertIn("Прервано", text)
        self.assertNotIn("Результат", text)
        self.assertIn("Замер прерван", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assert_cursor_restored(stdout)

    def test_parallel_ctrl_c_does_not_wait_for_slow_reads_to_fill_64_kib(self):
        # Filling a 64 KiB read would take over three seconds on this endpoint.
        code, stdout, stderr = self.run_terminal(
            "/ui-trickle", args=("--parallel", "--timeout", "5"),
            interrupt_at="Загрузка", cancel_timeout=2,
        )
        text = ANSI.sub("", stdout)
        self.assertEqual(code, 130)
        self.assertIn("Прервано", text)
        self.assertNotIn("Результат", text)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("Замер прерван", stderr)
        self.assert_cursor_restored(stdout)

    def test_plain_flag_and_dumb_terminal_use_original_text_output(self):
        for args, term in ((("--plain",), "xterm-256color"), ((), "dumb")):
            with self.subTest(args=args, term=term):
                code, stdout, stderr = self.run_terminal("/data", args=args, term=term)
                self.assertEqual((code, stderr), (0, ""))
                self.assertIn("Запрос 10/10:", stdout)
                self.assertIn("Скорость:", stdout)
                self.assertNotIn("\x1b", stdout)
                self.assertNotIn("Завершено:", stdout)


if __name__ == "__main__":
    unittest.main()
