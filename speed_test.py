"""Command-line entry point for the download speed measurement."""

import argparse
import math
import sys
from threading import TIMEOUT_MAX
from urllib.parse import urlsplit

from console_ui import RunDisplay
from measurement import REQUEST_COUNT, AttemptError, run_measurement
# Preserve the original download/statistics imports for existing Python callers.
from measurement import CHUNK_SIZE, DownloadError, Measurement, Statistics, calculate_statistics, download

# A conservative platform bound avoids overflow in socket timeout conversion.
MAX_TIMEOUT = int(TIMEOUT_MAX)


def http_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not any(char.isspace() for char in parsed.netloc)
        )
        # Accessing port validates its syntax and range.
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("укажите корректный HTTP- или HTTPS-адрес")
    return value


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("таймаут должен быть числом") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("таймаут должен быть положительным конечным числом")
    if timeout > MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"таймаут не должен превышать {MAX_TIMEOUT} секунд на этой платформе"
        )
    return timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Скорость скачивания: 10 запросов к одному URL."
    )
    parser.add_argument("url", metavar="URL", type=http_url, help="прямая ссылка на файл")
    parser.add_argument(
        "--plain", action="store_true", help="простой текстовый вывод без таблицы и анимации"
    )
    parser.add_argument(
        "--parallel", action="store_true",
        help="выполнить 10 запросов параллельно (по умолчанию: последовательно)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=30.0,
        metavar="SECONDS",
        help=(
            "таймаут соединения и ожидания данных в секундах (по умолчанию: 30); "
            "не ограничивает общую длительность скачивания; "
            f"максимум на этой платформе: {MAX_TIMEOUT}"
        ),
    )
    args = parser.parse_args(argv)
    display = RunDisplay(REQUEST_COUNT, plain=args.plain, parallel=args.parallel)
    try:
        with display:
            stats = run_measurement(
                args.url, args.timeout, parallel=args.parallel, observer=display,
            )
        display.show_summary(stats)
        return 0
    except AttemptError as exc:
        print(f"Ошибка запроса {exc.number}/{REQUEST_COUNT}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nЗамер прерван пользователем.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
