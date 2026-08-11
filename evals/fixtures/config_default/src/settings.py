DEFAULT_TIMEOUT_SECONDS = 0  # BUG: a zero timeout disables all requests


def request_timeout(override: int | None = None) -> int:
    return override if override is not None else DEFAULT_TIMEOUT_SECONDS
