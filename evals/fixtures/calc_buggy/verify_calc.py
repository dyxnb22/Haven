import sys

sys.path.insert(0, "src")
from calc import add, sub  # noqa: E402

assert add(2, 3) == 5, f"add(2, 3) returned {add(2, 3)}"
assert sub(5, 3) == 2
print("calc: ok")
sys.exit(0)
