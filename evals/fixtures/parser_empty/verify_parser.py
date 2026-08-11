import sys

sys.path.insert(0, "src")
from parser import parse_config  # noqa: E402

assert parse_config("") == {}, "empty input must return an empty dict"
assert parse_config("[config]\na = 1\nb = 2") == {"a": "1", "b": "2"}
print("parser: ok")
sys.exit(0)
