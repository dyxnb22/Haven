import sys

sys.path.insert(0, "src")
from text import canonicalize, clean_list, clean_pair  # noqa: E402

assert canonicalize("  A   B ") == "a b"
assert clean_list(["  X  Y "]) == ["x y"]
assert clean_pair(" P ", " Q ") == ("p", "q")
print("text: ok")
sys.exit(0)
