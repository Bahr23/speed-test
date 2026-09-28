"""Console observer: progress state, Rich rendering and plain text output."""

import time
from dataclasses import dataclass, replace
from threading import Lock

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from measurement import Measurement, Statistics

AUTHOR_TEXT = "Автор: @devrhab — https://t.me/devrhab"


@dataclass
class Attempt:
    status: str = "waiting"
    received: int = 0
    total: int | None = None
    started: float | None = None
    elapsed: float | None = None


LABELS = {
    "waiting": ("·", "Ожидание", "dim"),
    "connecting": ("", "Ожидание ответа", "cyan"),
    "downloading": ("", "Загрузка", "cyan"),
    "done": ("✓", "Готово", "green"),
    "error": ("✗", "Ошибка", "red"),
    "cancelled": ("■", "Прервано", "yellow"),
    "skipped": ("—", "Не запускалась", "dim"),
}


def format_volume(received: int, total: int | None) -> str:
    size = max(received, total or 0)
    divisor, unit = 1, "Б"
    for threshold, name in ((1_000, "КБ"), (1_000_000, "МБ"), (1_000_000_000, "ГБ")):
        if size >= threshold:
            divisor, unit = threshold, name
    precision = 0 if divisor == 1 else 1
    downloaded = f"{received / divisor:.{precision}f}"
    if total is not None:
        return f"{downloaded} / {total / divisor:.{precision}f} {unit}"
    return f"{downloaded} {unit}"


def format_duration(elapsed: float) -> str:
    """Keep the value and its unit visible in the seven-character column."""
    for divisor, unit in ((1, "с"), (60, "мин"), (3600, "ч"), (86400, "д")):
        for precision in (2, 1, 0):
            value = f"{elapsed / divisor:.{precision}f} {unit}"
            if len(value) <= 7:
                return value
    return f"{elapsed / 86400:.0e} д"


class RunDisplay:
    def __init__(
        self, count: int, *, plain: bool = False, parallel: bool = False,
        console: Console | None = None,
    ):
        self.console = console if console is not None else Console(highlight=False, markup=False)
        self.enabled = not plain and self.console.is_terminal and not self.console.is_dumb_terminal
        self.attempts = [Attempt() for _ in range(count)]
        self.parallel = parallel
        self.lock = Lock()
        self.spinner = Spinner("dots", style="cyan")
        self.live = Live(
            console=self.console,
            get_renderable=self.render,
            refresh_per_second=10,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        ) if self.enabled else None

    def __enter__(self):
        if self.live is not None:
            self.live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Execution owns attempt outcomes; this context owns only the animation.
        if self.live is not None:
            self.live.stop()

    def start_attempt(self, number: int) -> None:
        with self.lock:
            if self.attempts[number - 1].status != "waiting":
                return
            self.attempts[number - 1] = Attempt(status="connecting", started=time.perf_counter())

    def update(self, number: int, received_bytes: int, total_bytes: int | None) -> None:
        # Never render on the network thread: Live reads a snapshot at 10 Hz.
        if not self.enabled:
            return
        with self.lock:
            row = self.attempts[number - 1]
            if row.status in {"connecting", "downloading"}:
                row.status = "downloading"
                row.received = received_bytes
                row.total = total_bytes

    def finish_attempt(self, number: int, result: Measurement) -> None:
        with self.lock:
            row = self.attempts[number - 1]
            if row.status not in {"connecting", "downloading"}:
                return
            row.status = "done"
            row.received = result.size_bytes
            row.elapsed = result.elapsed_seconds
        if not self.enabled:
            print(
                f"Запрос {number}/{len(self.attempts)}: "
                f"{result.elapsed_seconds:.3f} с, {result.size_bytes} байт, "
                f"{result.size_bytes / result.elapsed_seconds / 1_000_000:.3f} МБ/с",
                file=self.console.file,
                flush=True,
            )

    def fail_attempt(self, number: int) -> None:
        with self.lock:
            row = self.attempts[number - 1]
            if row.status in {"connecting", "downloading"}:
                row.status = "error"
                row.elapsed = time.perf_counter() - row.started

    def abort(self) -> None:
        with self.lock:
            for row in self.attempts:
                if row.status in {"connecting", "downloading"}:
                    row.status = "cancelled"
                    row.elapsed = time.perf_counter() - row.started
                elif row.status == "waiting":
                    row.status = "skipped"

    def render(self) -> Group:
        with self.lock:
            rows = [replace(row) for row in self.attempts]
        compact = self.console.width < 80
        now = time.perf_counter()
        table = Table(
            box=box.ROUNDED,
            border_style="bright_black",
            header_style="bold",
            expand=True,
            padding=(0, 0) if compact else (0, 1),
        )
        table.add_column("№", width=2, justify="right", no_wrap=True)
        table.add_column("Статус", width=17, no_wrap=True)
        table.add_column("%" if compact else "Прогресс", ratio=1,
                         min_width=4 if compact else 8, justify="right", no_wrap=True)
        table.add_column("Объём", justify="right", no_wrap=True)
        table.add_column("Время", width=7, justify="right", no_wrap=True)
        table.add_column("МБ/с", width=7, justify="right", no_wrap=True)
        for number, row in enumerate(rows, 1):
            icon, label, style = LABELS[row.status]
            if row.status in {"connecting", "downloading"}:
                status = Text.assemble(self.spinner.render(now), " ", label, style=style)
            else:
                status = Text(f"{icon} {label}", style=style)

            progress = Text("—", style="dim")
            if row.total is not None:
                # Only a successfully completed read can display 100%.
                percent = 100 if row.status == "done" else min(99, row.received * 100 // row.total)
                progress = Text(f"{percent}%", style=style)
                if not compact:
                    bar = Table.grid(expand=True, padding=(0, 1))
                    bar.add_column(ratio=1)
                    bar.add_column(width=4, justify="right")
                    bar.add_row(
                        ProgressBar(
                            total=100, completed=percent, width=None,
                            style="bright_black", complete_style=style, finished_style=style,
                        ),
                        progress,
                    )
                    progress = bar

            volume = "—" if row.started is None else format_volume(row.received, row.total)
            elapsed = row.elapsed
            if elapsed is None and row.started is not None:
                elapsed = now - row.started
            duration = "—" if elapsed is None else format_duration(elapsed)
            speed = "—"
            if elapsed is not None:
                rate = row.received / max(elapsed, 1e-9) / 1_000_000
                speed = f"{rate:.3f}"
                if len(speed) > 7:
                    speed = f"{rate:.1f}"
                if len(speed) > 7:
                    speed = f"{rate:.1e}"
            table.add_row(str(number), status, progress, volume, duration, speed, style=style)

        completed = sum(row.status == "done" for row in rows)
        mode = "Параллельно" if self.parallel else "Последовательно"
        return Group(
            Text("Замер скорости скачивания", style="bold cyan"),
            Text(f"{mode} · Завершено: {completed}/{len(rows)}", style="dim"),
            table,
        )

    def show_summary(self, stats: Statistics) -> None:
        if not self.enabled:
            print(
                f"\nСреднее время запроса: {stats.average_seconds:.3f} с\n"
                f"Скачано: {stats.total_bytes} байт ({stats.total_bytes / 1_000_000:.3f} МБ)\n"
                f"Скорость: {stats.megabytes_per_second:.3f} МБ/с "
                f"({stats.megabits_per_second:.3f} Мбит/с)\n\n"
                f"{AUTHOR_TEXT}",
                file=self.console.file,
            )
            return
        summary = Text()
        summary.append(f"{stats.megabytes_per_second:.3f} МБ/с", style="bold cyan")
        summary.append(f"  ({stats.megabits_per_second:.3f} Мбит/с)\n\n")
        summary.append(f"Среднее время запроса: {stats.average_seconds:.3f} с\n")
        summary.append(f"Скачано: {stats.total_bytes} байт ({stats.total_bytes / 1_000_000:.3f} МБ)")
        summary.append(f"\n\n{AUTHOR_TEXT}", style="dim")
        self.console.print(Panel(summary, title="Результат", border_style="green", padding=(1, 2)))
