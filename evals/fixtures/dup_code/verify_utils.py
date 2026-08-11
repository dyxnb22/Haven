import sys

sys.path.insert(0, "src")
from utils import normalize_name, normalize_title  # noqa: E402

assert normalize_name("  Ada   Lovelace ") == "ada lovelace"
assert normalize_title("  The   Analytical Engine ") == "the analytical engine"
assert normalize_title("SAME  input") == normalize_name("SAME  input")
print("utils: ok")
sys.exit(0)
