import sys

sys.path.insert(0, "src")
from wide import add

sys.exit(0 if add(2, 3) == 5 else 1)
