def normalize_name(value: str) -> str:
    cleaned = value.strip().lower()
    return " ".join(cleaned.split())


def normalize_title(value: str) -> str:
    cleaned = value.strip().lower()
    return " ".join(cleaned.split())
