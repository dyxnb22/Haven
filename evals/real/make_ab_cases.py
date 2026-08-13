"""Emit the two arms of the repetition-nudge A/B.

Both directories hold the same seven tier-3 cases and differ in exactly one
JSON field, so any step-count difference is attributable to the nudge and not
to the task set. The case ids are fixed in advance (they are the ones that hit
the step ceiling in earlier live reports) to keep the comparison pre-registered
rather than chosen after seeing the numbers.

Case directories sit beside `cases/` because the runner resolves fixtures as
`cases_dir.parent / "fixtures"`.
"""

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "cases"
CONTROL = HERE / "cases-nudge-control"
TREATMENT = HERE / "cases-nudge-treatment"
CASE_IDS = (
    "t3-click-show-default",
    "t3-click-bool-onoff",
    "t3-click-echo-stderr",
    "t3-click-nargs-order",
    "t3-click-range-clamp",
    "t3-jinja-default-filter",
    "t3-rich-truncate-ellipsis",
)


def main() -> None:
    for dest in (CONTROL, TREATMENT):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

    for case_id in CASE_IDS:
        path = SRC / f"{case_id}.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        # Treatment is the shipped default, so its key is omitted rather than
        # written as true: the arms then differ by one line of JSON.
        (TREATMENT / path.name).write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
        case["repeat_nudge"] = False
        (CONTROL / path.name).write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {len(CASE_IDS)} cases to each of {CONTROL.name} and {TREATMENT.name}")


if __name__ == "__main__":
    main()
