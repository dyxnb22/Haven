def parse_config(text: str) -> dict[str, str]:
    header, *lines = text.splitlines()
    if header != "[config]":
        raise ValueError("missing [config] header")
    result: dict[str, str] = {}
    for line in lines:
        if line.strip():
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result
