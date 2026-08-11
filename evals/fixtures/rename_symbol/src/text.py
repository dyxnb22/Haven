def normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def clean_list(values: list[str]) -> list[str]:
    return [normalize(v) for v in values]


def clean_pair(a: str, b: str) -> tuple[str, str]:
    return normalize(a), normalize(b)
