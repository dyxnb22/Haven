import sys

sys.path.insert(0, "src")
from settings import request_timeout  # noqa: E402

assert request_timeout() == 30, f"default timeout is {request_timeout()}, expected 30"
assert request_timeout(5) == 5
print("settings: ok")
sys.exit(0)
