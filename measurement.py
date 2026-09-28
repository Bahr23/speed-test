"""Download measurements and run lifecycle, independent of console rendering."""

import math
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
from threading import Event
from typing import Protocol

import requests
from urllib3.exceptions import HTTPError as StreamError

REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class Measurement:
    size_bytes: int
    elapsed_seconds: float


@dataclass(frozen=True)
class Statistics:
    total_bytes: int
    average_seconds: float
    megabytes_per_second: float
    megabits_per_second: float


class DownloadError(Exception):
    """A request could not produce a complete, usable measurement."""


class AttemptError(DownloadError):
    def __init__(self, number: int, cause: DownloadError):
        super().__init__(str(cause))
        self.number = number


class RunObserver(Protocol):
    """Progress notifications; no terminal, rendering, or configuration required.

    start_attempt/update can be called by worker threads and must be thread-safe.
    They should only update state. Other notifications run on the calling thread.
    A failed run calls fail_attempt (for a download error), then abort, before
    joining workers. After abort, late updates must not revive terminal states.
    """

    def start_attempt(self, number: int) -> None: ...
    def update(self, number: int, received_bytes: int, total_bytes: int | None) -> None: ...
    def finish_attempt(self, number: int, result: Measurement) -> None: ...
    def fail_attempt(self, number: int) -> None: ...
    def abort(self) -> None: ...


def download(
    session: requests.Session,
    url: str,
    timeout: float,
    *,
    on_progress: Callable[[int, int | None], None] | None = None,
    cancel_event: Event | None = None,
) -> Measurement:
    """Count body bytes without decompression or saving the response to disk."""
    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()

    check_cancelled()
    started = time.perf_counter()
    try:
        with session.get(
            url,
            stream=True,
            timeout=timeout,
            headers={"Accept-Encoding": "identity"},
        ) as response:
            check_cancelled()
            response.raise_for_status()
            total = None
            if on_progress is not None:
                length = response.headers.get("Content-Length", "").strip()
                transfer_encoding = response.headers.get("Transfer-Encoding", "").lower()
                if "chunked" not in transfer_encoding and length.isascii() and length.isdecimal():
                    try:
                        total = int(length) or None
                    except ValueError:
                        pass  # Excessively long header: keep progress indeterminate.
                on_progress(0, total)
            # Reading raw also handles servers that ignore Accept-Encoding.
            size_bytes = 0
            while True:
                check_cancelled()
                # read1 returns available data without waiting to fill a 64 KiB buffer.
                chunk = response.raw.read1(CHUNK_SIZE, decode_content=False)
                check_cancelled()
                if not chunk:
                    break
                size_bytes += len(chunk)
                if on_progress is not None:
                    on_progress(size_bytes, total)
            elapsed_seconds = time.perf_counter() - started
            check_cancelled()
    except (requests.RequestException, StreamError) as exc:
        # raw exposes urllib3 errors directly, bypassing Requests' wrappers.
        raise DownloadError(str(exc)) from exc

    if size_bytes == 0:
        raise DownloadError("сервер вернул пустое тело ответа")
    if elapsed_seconds <= 0:
        raise DownloadError("не удалось измерить длительность запроса")
    return Measurement(size_bytes, elapsed_seconds)


def calculate_statistics(
    measurements: list[Measurement], *, wall_seconds: float | None = None,
) -> Statistics:
    """Use the group's wall time for overlapping requests, summed time otherwise."""
    if not measurements:
        raise ValueError("Для расчёта нужны результаты скачиваний")
    total_bytes = sum(item.size_bytes for item in measurements)
    total_seconds = sum(item.elapsed_seconds for item in measurements)
    if total_seconds <= 0:
        raise ValueError("Общее время должно быть положительным")
    speed_seconds = total_seconds if wall_seconds is None else wall_seconds
    if not math.isfinite(speed_seconds) or speed_seconds <= 0:
        raise ValueError("Время замера должно быть положительным конечным числом")
    megabytes_per_second = total_bytes / speed_seconds / 1_000_000
    return Statistics(
        total_bytes=total_bytes,
        average_seconds=total_seconds / len(measurements),
        megabytes_per_second=megabytes_per_second,
        megabits_per_second=megabytes_per_second * 8,
    )


def run_measurement(
    url: str, timeout: float, *, parallel: bool = False,
    observer: RunObserver | None = None,
) -> Statistics:
    """Run ten downloads, owning sessions, cancellation and worker cleanup.

    Return statistics only after all ten successes. AttemptError identifies a
    failed download; KeyboardInterrupt propagates after cleanup. Notifications
    are optional. A blocked read still needs data or its network timeout to exit.
    """
    stop = Event()
    executor = None
    futures = {}
    future = None

    def attempt(session: requests.Session, number: int) -> Measurement:
        if stop.is_set():
            raise CancelledError()
        if observer is not None:
            observer.start_attempt(number)
        try:
            return download(
                session, url, timeout,
                on_progress=partial(observer.update, number) if observer is not None else None,
                cancel_event=stop,
            )
        except DownloadError as exc:
            raise AttemptError(number, exc) from exc

    def worker(number: int) -> tuple[Measurement, float]:
        # Each worker owns its session; no shared mutable session state.
        with requests.Session() as session:
            result = attempt(session, number)
            finished_at = time.perf_counter()
        return result, finished_at

    try:
        measurements = []
        wall_seconds = None
        if parallel:
            executor = ThreadPoolExecutor(max_workers=REQUEST_COUNT)
            started = time.perf_counter()
            last_finished = started
            futures = {
                executor.submit(worker, number): number
                for number in range(1, REQUEST_COUNT + 1)
            }
            for future in as_completed(futures):
                result, finished_at = future.result()
                last_finished = max(last_finished, finished_at)
                measurements.append(result)
                if observer is not None:
                    observer.finish_attempt(futures[future], result)
            wall_seconds = last_finished - started
        else:
            with requests.Session() as session:
                for number in range(1, REQUEST_COUNT + 1):
                    result = attempt(session, number)
                    measurements.append(result)
                    if observer is not None:
                        observer.finish_attempt(number, result)
        return calculate_statistics(measurements, wall_seconds=wall_seconds)
    except BaseException as exc:
        stop.set()
        if observer is not None:
            if isinstance(exc, AttemptError):
                observer.fail_attempt(exc.number)
            observer.abort()
        raise
    finally:
        try:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
        finally:
            # Future exceptions reference this frame. Break that cycle so response
            # pools are released without waiting for cyclic garbage collection.
            futures.clear()
            future = None
