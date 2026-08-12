"""Real-repository task specs for the live success-rate measurement.

Each task takes a pinned real project, injects one (or a few) surgical bugs into
its source, and asks the agent to fix the described symptom. The project's own
test suite — run through a registered `verify` recipe — is the oracle: the run
succeeds only when the Evidence Gate sees a diff followed by a green check, so
"passed" means the agent actually repaired the behavior, not that it claimed to.

This is bug-injection rather than green-field feature work on purpose: it yields
an objective, pre-existing pass/fail signal grounded in each project's real
tests, with no bespoke grader to argue with. `build.py` turns these specs into
fixtures + case JSON and, with --verify, proves each task is red before the fix
and green when reverted.

`repos/<dir>` are shallow clones pinned in `repos.lock`. Nothing here is
committed as a fixture; fixtures and cases are generated locally (gitignored),
because they are multi-megabyte copies of third-party code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PY = "{python}"  # the eval harness substitutes the interpreter


@dataclass(frozen=True)
class Repo:
    #: Directory under evals/real/repos.
    dir: str
    #: pytest argv (after `{python} -m pytest`) that is green on the clean clone
    #: and red once the task's bug is present.
    verify: tuple[str, ...]
    #: A path (relative to the repo root) to prepend to sys.path via a generated
    #: root conftest.py, for src-layout projects that are not installed.
    src_path: str | None = None
    timeout: float = 180.0


REPOS: dict[str, Repo] = {
    # `-x` (stop at first failure) bounds the cost of a still-broken check: a red
    # suite aborts fast instead of formatting thousands of tracebacks, while a
    # green suite (no failures) still runs every test.
    "jmespath": Repo(
        dir="jmespath.py",
        verify=("-q", "-x", "-p", "no:cacheprovider", "tests"),
    ),
    "idna": Repo(
        dir="idna",
        verify=("-q", "-x", "-p", "no:cacheprovider", "tests"),
    ),
    "wcwidth": Repo(
        dir="wcwidth",
        # `-o addopts=` drops the project's tox.ini coverage flags (they need
        # pytest-cov, absent here). test_package_version needs installed dist
        # metadata, absent in a raw clone, so it is excluded from the oracle.
        verify=(
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-k",
            "not package_version",
            "tests",
        ),
    ),
    "tomli": Repo(
        dir="tomli",
        verify=("-q", "-x", "-p", "no:cacheprovider", "tests"),
        src_path="src",
    ),
    "tabulate": Repo(
        dir="tabulate",
        verify=("-q", "-x", "-p", "no:cacheprovider", "test"),
    ),
    # ---- tier 3: larger repositories (10k+ source lines) -----------------
    # `-p no:respx` on every tier-3 recipe: Haven's dev venv ships respx, whose
    # pytest plugin imports httpx, whose optional CLI import chain pulls the
    # *installed* click/rich/pygments into sys.modules before the fixture's
    # conftest shim can put the checkout first. Blocking the plugin keeps the
    # checkout authoritative; build.py --verify proves it (a bug injected into
    # the checkout must turn the suite red, which is impossible if the suite
    # were importing site-packages).
    "click": Repo(
        dir="click",
        # Two exclusions, both environmental (same class as wcwidth's
        # package_version): test_path_dash_no_byteswarning spawns
        # `python -bb -c "import click"` in a subprocess, bypassing the
        # conftest shim and testing the *installed* click; echo_via_pager
        # kills its pager child process, which the check sandbox (Seatbelt)
        # denies — green raw, red sandboxed, so it cannot be an oracle.
        verify=(
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:respx",
            "-k",
            "not path_dash_no_byteswarning and not echo_via_pager",
            "tests",
        ),
        src_path="src",
    ),
    "jinja": Repo(
        dir="jinja",
        verify=("-q", "-x", "-p", "no:cacheprovider", "-p", "no:respx", "tests"),
        src_path="src",
    ),
    "pygments": Repo(
        dir="pygments",
        verify=("-q", "-x", "-p", "no:cacheprovider", "-p", "no:respx", "tests"),
        timeout=300.0,
    ),
    "rich": Repo(
        dir="rich",
        verify=("-q", "-x", "-p", "no:cacheprovider", "-p", "no:respx", "tests"),
    ),
}


@dataclass(frozen=True)
class Task:
    id: str
    repo: str
    goal: str
    #: (relative path, exact snippet to find, replacement) — the injected bug.
    inject: tuple[tuple[str, str, str], ...]
    #: Files the fix may touch; anything else the eval flags as out of scope.
    allowed: tuple[str, ...]
    difficulty: str  # easy | medium | hard
    tags: tuple[str, ...] = field(default_factory=tuple)


TASKS: list[Task] = [
    # ---- jmespath: built-in functions -----------------------------------
    Task(
        "jmespath-starts-with",
        "jmespath",
        "The JMESPath built-in function `starts_with(subject, prefix)` is "
        "returning the wrong answer (it reports a match based on the end of the "
        "string, not the start). Fix the implementation, then run the `verify` "
        "check.",
        (
            (
                "jmespath/functions.py",
                "        return search.startswith(suffix)",
                "        return search.endswith(suffix)",
            ),
        ),
        ("jmespath/functions.py",),
        "easy",
        ("operator",),
    ),
    Task(
        "jmespath-ends-with",
        "jmespath",
        "The JMESPath built-in `ends_with(subject, suffix)` is checking the "
        "start of the string instead of the end. Correct it and verify.",
        (
            (
                "jmespath/functions.py",
                "        return search.endswith(suffix)",
                "        return search.startswith(suffix)",
            ),
        ),
        ("jmespath/functions.py",),
        "easy",
        ("operator",),
    ),
    Task(
        "jmespath-abs",
        "jmespath",
        "The JMESPath `abs()` function returns its argument unchanged instead of "
        "the absolute value. Fix it and run the `verify` check.",
        (("jmespath/functions.py", "        return abs(arg)", "        return arg"),),
        ("jmespath/functions.py",),
        "easy",
        ("operator",),
    ),
    Task(
        "jmespath-ceil",
        "jmespath",
        "The JMESPath `ceil()` function is rounding down instead of up. Fix it "
        "and verify with the `verify` check.",
        (
            (
                "jmespath/functions.py",
                "        return math.ceil(arg)",
                "        return math.floor(arg)",
            ),
        ),
        ("jmespath/functions.py",),
        "medium",
        ("numeric",),
    ),
    Task(
        "jmespath-contains",
        "jmespath",
        "The JMESPath `contains(subject, search)` function returns the opposite "
        "of the correct result. Fix it and run the `verify` check.",
        (
            (
                "jmespath/functions.py",
                "        return search in subject",
                "        return search not in subject",
            ),
        ),
        ("jmespath/functions.py",),
        "easy",
        ("operator",),
    ),
    Task(
        "jmespath-length",
        "jmespath",
        "The JMESPath `length()` function is returning a count that is one too "
        "small. Fix it and verify.",
        (("jmespath/functions.py", "        return len(arg)", "        return len(arg) - 1"),),
        ("jmespath/functions.py",),
        "easy",
        ("off-by-one",),
    ),
    Task(
        "jmespath-reverse",
        "jmespath",
        "The JMESPath `reverse()` function no longer reverses strings (it returns "
        "the string unchanged); reversing lists still works. Fix the string case "
        "and run the `verify` check.",
        (("jmespath/functions.py", "            return arg[::-1]", "            return arg"),),
        ("jmespath/functions.py",),
        "medium",
        ("string",),
    ),
    Task(
        "jmespath-merge",
        "jmespath",
        "The JMESPath `merge()` function resolves key conflicts with the wrong "
        "precedence: when two objects share a key, the earlier object wins, but "
        "the later one should. Fix it and verify.",
        (
            (
                "jmespath/functions.py",
                "        merged = {}\n        for arg in arguments:\n"
                "            merged.update(arg)",
                "        merged = {}\n        for arg in reversed(arguments):\n"
                "            merged.update(arg)",
            ),
        ),
        ("jmespath/functions.py",),
        "medium",
        ("ordering",),
    ),
    Task(
        "jmespath-join",
        "jmespath",
        "The JMESPath `join(glue, array)` function is concatenating the elements "
        "in reverse order. Fix it and run the `verify` check.",
        (
            (
                "jmespath/functions.py",
                "        return separator.join(array)",
                "        return separator.join(reversed(array))",
            ),
        ),
        ("jmespath/functions.py",),
        "easy",
        ("ordering",),
    ),
    Task(
        "jmespath-sort",
        "jmespath",
        "The JMESPath `sort()` function returns results in descending order; it "
        "should sort ascending. Fix it and verify.",
        (
            (
                "jmespath/functions.py",
                "        return list(sorted(arg))",
                "        return list(reversed(sorted(arg)))",
            ),
        ),
        ("jmespath/functions.py",),
        "easy",
        ("ordering",),
    ),
    # ---- jmespath: interpreter (visitor) --------------------------------
    Task(
        "jmespath-not-expression",
        "jmespath",
        "The JMESPath `!` (not) operator returns the value's truthiness instead "
        "of negating it. Fix the interpreter and run the `verify` check.",
        (
            (
                "jmespath/visitor.py",
                "        return not original_result",
                "        return original_result",
            ),
        ),
        ("jmespath/visitor.py",),
        "medium",
        ("interpreter",),
    ),
    Task(
        "jmespath-or-expression",
        "jmespath",
        "The JMESPath `||` (or) operator picks the wrong branch: it falls through "
        "to the right-hand side when the left is truthy, not when it is falsy. "
        "Fix the interpreter and verify.",
        (
            (
                "jmespath/visitor.py",
                "        matched = self.visit(node['children'][0], value)\n"
                "        if self._is_false(matched):\n"
                "            matched = self.visit(node['children'][1], value)\n"
                "        return matched",
                "        matched = self.visit(node['children'][0], value)\n"
                "        if self._is_true(matched):\n"
                "            matched = self.visit(node['children'][1], value)\n"
                "        return matched",
            ),
        ),
        ("jmespath/visitor.py",),
        "medium",
        ("interpreter",),
    ),
    Task(
        "jmespath-and-expression",
        "jmespath",
        "The JMESPath `&&` (and) operator short-circuits on the wrong condition. "
        "Fix the interpreter and run the `verify` check.",
        (
            (
                "jmespath/visitor.py",
                "        matched = self.visit(node['children'][0], value)\n"
                "        if self._is_false(matched):\n            return matched",
                "        matched = self.visit(node['children'][0], value)\n"
                "        if self._is_true(matched):\n            return matched",
            ),
        ),
        ("jmespath/visitor.py",),
        "medium",
        ("interpreter",),
    ),
    Task(
        "jmespath-comparator-lt",
        "jmespath",
        "The JMESPath `<` comparator is behaving like `<=` (it treats equal "
        "values as less-than). Fix the comparator table and verify.",
        (("jmespath/visitor.py", "        'lt': operator.lt,", "        'lt': operator.le,"),),
        ("jmespath/visitor.py",),
        "hard",
        ("interpreter",),
    ),
    Task(
        "jmespath-filter-projection",
        "jmespath",
        "A JMESPath filter projection `[?expr]` is collecting the elements that "
        "evaluate to null and dropping the rest — the null check is inverted. "
        "Fix the interpreter and run the `verify` check.",
        (
            (
                "jmespath/visitor.py",
                "                current = self.visit(node['children'][1], element)\n"
                "                if current is not None:\n"
                "                    collected.append(current)",
                "                current = self.visit(node['children'][1], element)\n"
                "                if current is None:\n"
                "                    collected.append(current)",
            ),
        ),
        ("jmespath/visitor.py",),
        "medium",
        ("interpreter",),
    ),
    Task(
        "jmespath-flatten",
        "jmespath",
        "The JMESPath flatten operator `[]` nests sub-lists instead of merging "
        "them, and drops scalars. The extend/append logic is swapped. Fix it and "
        "verify.",
        (
            (
                "jmespath/visitor.py",
                "            if isinstance(element, list):\n"
                "                merged_list.extend(element)\n"
                "            else:\n"
                "                merged_list.append(element)",
                "            if isinstance(element, list):\n"
                "                merged_list.append(element)\n"
                "            else:\n"
                "                merged_list.extend(element)",
            ),
        ),
        ("jmespath/visitor.py",),
        "medium",
        ("interpreter",),
    ),
    # ---- idna -----------------------------------------------------------
    Task(
        "idna-label-length",
        "idna",
        "IDNA label length validation is off by one: it accepts labels of 64 "
        "octets when the DNS limit is 63. Fix `valid_label_length` and run the "
        "`verify` check.",
        (("idna/core.py", "    return len(label) <= 63", "    return len(label) <= 64"),),
        ("idna/core.py",),
        "medium",
        ("off-by-one", "validation"),
    ),
    Task(
        "idna-string-length",
        "idna",
        "IDNA total-domain length validation has the trailing-dot logic "
        "backwards, so it applies the wrong octet limit. Fix `valid_string_length` "
        "and verify.",
        (
            (
                "idna/core.py",
                "    return len(domain) <= (254 if trailing_dot else 253)",
                "    return len(domain) <= (253 if trailing_dot else 254)",
            ),
        ),
        ("idna/core.py",),
        "medium",
        ("validation",),
    ),
    Task(
        "idna-encode-range",
        "idna",
        "The interval-range encoding in idna packs the start value into the wrong "
        "bit position (16 bits instead of 32), so codepoint range lookups are "
        "corrupted. Fix `_encode_range` in intranges.py and run the `verify` "
        "check.",
        (
            (
                "idna/intranges.py",
                "    return (start << 32) | end",
                "    return (start << 16) | end",
            ),
        ),
        ("idna/intranges.py",),
        "medium",
        ("bit-twiddling",),
    ),
    Task(
        "idna-intranges-contain",
        "idna",
        "Membership testing over encoded interval ranges is broken: codepoints "
        "that fall inside a range are reported as absent. Fix `intranges_contain` "
        "and run the `verify` check.",
        (
            (
                "idna/intranges.py",
                "        left, right = _decode_range(ranges[pos - 1])\n"
                "        if left <= int_ < right:\n            return True",
                "        left, right = _decode_range(ranges[pos - 1])\n"
                "        if left <= int_ < right:\n            return False",
            ),
        ),
        ("idna/intranges.py",),
        "hard",
        ("search",),
    ),
    Task(
        "idna-alabel-prefix",
        "idna",
        "The ACE prefix used for internationalized labels is wrong (`xn-` instead "
        "of the standard `xn--`), so encoding and decoding of A-labels is broken. "
        "Fix the constant and run the `verify` check.",
        (("idna/core.py", '_alabel_prefix = b"xn--"', '_alabel_prefix = b"xn-"'),),
        ("idna/core.py",),
        "medium",
        ("constant",),
    ),
    # ---- wcwidth --------------------------------------------------------
    Task(
        "wcwidth-ascii",
        "wcwidth",
        "wcwidth reports width 2 for ordinary printable ASCII characters; they "
        "should be width 1. Fix the fast path in _wcwidth.py and run the `verify` "
        "check.",
        (
            (
                "wcwidth/_wcwidth.py",
                "    if 32 <= ucs < 0x7f:\n        return 1",
                "    if 32 <= ucs < 0x7f:\n        return 2",
            ),
        ),
        ("wcwidth/_wcwidth.py",),
        "easy",
        ("width",),
    ),
    Task(
        "wcwidth-wide",
        "wcwidth",
        "Wide East-Asian characters are being reported as width 1 instead of 2. "
        "Fix wcwidth and verify.",
        (
            (
                "wcwidth/_wcwidth.py",
                "    if bisearch(ucs, _WIDE_EASTASIAN_TABLE):\n        return 2",
                "    if bisearch(ucs, _WIDE_EASTASIAN_TABLE):\n        return 1",
            ),
        ),
        ("wcwidth/_wcwidth.py",),
        "medium",
        ("width",),
    ),
    Task(
        "wcwidth-zero",
        "wcwidth",
        "Zero-width (combining) characters are being counted as width 1 instead "
        "of 0. Fix wcwidth and run the `verify` check.",
        (
            (
                "wcwidth/_wcwidth.py",
                "    if bisearch(ucs, _ZERO_WIDTH_TABLE):\n        return 0",
                "    if bisearch(ucs, _ZERO_WIDTH_TABLE):\n        return 1",
            ),
        ),
        ("wcwidth/_wcwidth.py",),
        "medium",
        ("width",),
    ),
    Task(
        "wcwidth-bisearch",
        "wcwidth",
        "The binary search over Unicode interval tables never reports a hit (it "
        "returns 0 even when the codepoint is inside a range), so wide and "
        "zero-width lookups all fail. Fix `bisearch` and verify.",
        (
            (
                "wcwidth/bisearch.py",
                "        else:\n            return 1",
                "        else:\n            return 0",
            ),
        ),
        ("wcwidth/bisearch.py",),
        "medium",
        ("search",),
    ),
    # ---- tomli (src layout) ---------------------------------------------
    Task(
        "tomli-tz-sign",
        "tomli",
        "Parsing a TOML datetime with a timezone offset applies the offset in the "
        "wrong direction (+ and - are swapped). Fix `cached_tz` in _re.py and run "
        "the `verify` check.",
        (
            (
                "src/tomli/_re.py",
                '    sign = 1 if sign_str == "+" else -1',
                '    sign = -1 if sign_str == "+" else 1',
            ),
        ),
        ("src/tomli/_re.py",),
        "medium",
        ("datetime",),
    ),
    Task(
        "tomli-number-base",
        "tomli",
        "TOML integers written in hex/octal/binary (0x, 0o, 0b) fail to parse "
        "because the parser forces base 10. Fix `match_to_number` in _re.py and "
        "verify.",
        (
            (
                "src/tomli/_re.py",
                "    return int(match.group(), 0)",
                "    return int(match.group())",
            ),
        ),
        ("src/tomli/_re.py",),
        "medium",
        ("numeric",),
    ),
    Task(
        "tomli-localtime-micros",
        "tomli",
        "Fractional seconds on a bare TOML local-time value are parsed with the "
        "wrong padding, so e.g. `.1` becomes the wrong number of microseconds. "
        "Fix `match_to_localtime` in _re.py and run the `verify` check.",
        (
            (
                "src/tomli/_re.py",
                "def match_to_localtime(match: re.Match[str]) -> time:\n"
                "    hour_str, minute_str, sec_str, micros_str = match.groups()\n"
                "    sec = int(sec_str) if sec_str else 0\n"
                '    micros = int(micros_str.ljust(6, "0")) if micros_str else 0',
                "def match_to_localtime(match: re.Match[str]) -> time:\n"
                "    hour_str, minute_str, sec_str, micros_str = match.groups()\n"
                "    sec = int(sec_str) if sec_str else 0\n"
                '    micros = int(micros_str.rjust(6, "0")) if micros_str else 0',
            ),
        ),
        ("src/tomli/_re.py",),
        "hard",
        ("datetime",),
    ),
    # ---- tabulate -------------------------------------------------------
    Task(
        "tabulate-padleft",
        "tabulate",
        "Right-aligned columns in tabulate are coming out left-aligned: the "
        "`_padleft` helper pads on the wrong side. Fix it and run the `verify` "
        "check.",
        (("tabulate/__init__.py", '    fmt = f"{{0:>{width}s}}"', '    fmt = f"{{0:<{width}s}}"'),),
        ("tabulate/__init__.py",),
        "medium",
        ("formatting",),
    ),
    Task(
        "tabulate-padright",
        "tabulate",
        "Left-aligned columns in tabulate are coming out right-aligned: the "
        "`_padright` helper pads on the wrong side. Fix it and verify.",
        (("tabulate/__init__.py", '    fmt = f"{{0:<{width}s}}"', '    fmt = f"{{0:>{width}s}}"'),),
        ("tabulate/__init__.py",),
        "medium",
        ("formatting",),
    ),
    Task(
        "tabulate-padboth",
        "tabulate",
        "Centered columns in tabulate are not centered — `_padboth` uses "
        "left-alignment formatting. Fix it and run the `verify` check.",
        (("tabulate/__init__.py", '    fmt = f"{{0:^{width}s}}"', '    fmt = f"{{0:<{width}s}}"'),),
        ("tabulate/__init__.py",),
        "medium",
        ("formatting",),
    ),
    # ---- tier 2: green-field (a whole function is missing) ---------------
    # The bug-injection tier saturated at 31/31, so these delete an entire
    # function and ask the agent to write it back from call sites and tests —
    # small but genuine green-field work with the project's own suite as the
    # spec.
    Task(
        "impl-jmespath-ends-with",
        "jmespath",
        "The JMESPath built-in function `ends_with(subject, suffix)` is missing "
        "from this build: expressions using it fail with UnknownFunctionError. "
        "Implement it (it must return whether the subject string ends with the "
        "given suffix, both arguments strings) and run the `verify` check.",
        (
            (
                "jmespath/functions.py",
                "    @signature({'types': ['string']}, {'types': ['string']})\n"
                "    def _func_ends_with(self, search, suffix):\n"
                "        return search.endswith(suffix)\n\n",
                "",
            ),
        ),
        ("jmespath/functions.py",),
        "hard",
        ("green-field",),
    ),
    Task(
        "impl-jmespath-map",
        "jmespath",
        "The JMESPath built-in `map(&expr, array)` is missing from this build: "
        "expressions using it fail with UnknownFunctionError. Implement it — it "
        "applies the given expression reference to every element of the array "
        "and returns the list of results (nulls included). Nearby functions like "
        "sort_by show how expression references are evaluated. Then run the "
        "`verify` check.",
        (
            (
                "jmespath/functions.py",
                "    @signature({'types': ['expref']}, {'types': ['array']})\n"
                "    def _func_map(self, expref, arg):\n"
                "        result = []\n"
                "        for element in arg:\n"
                "            result.append(expref.visit(expref.expression, element))\n"
                "        return result\n\n",
                "",
            ),
        ),
        ("jmespath/functions.py",),
        "hard",
        ("green-field",),
    ),
    Task(
        "impl-idna-label-length",
        "idna",
        "This idna build is missing the label length validation helper: "
        "encoding raises NameError: name 'valid_label_length' is not defined. "
        "Implement the missing function with the correct DNS length rule for "
        "labels (both str U-labels and bytes A-labels are passed to it) and run "
        "the `verify` check.",
        (
            (
                "idna/core.py",
                "def valid_label_length(label: Union[bytes, str]) -> bool:\n"
                '    """Check that a label does not exceed the maximum permitted length.\n'
                "\n"
                "    Per :rfc:`1035` (and :rfc:`5891` §4.2.4) a DNS label must not exceed\n"
                "    63 octets. The argument may be either a :class:`str` (a U-label, where\n"
                "    length is measured in characters) or :class:`bytes` (an A-label, where\n"
                "    length is measured in octets).\n"
                "\n"
                "    :param label: The label to check.\n"
                "    :returns: ``True`` if the label is within the length limit, otherwise\n"
                "        ``False``.\n"
                '    """\n'
                "    return len(label) <= 63\n\n\n",
                "",
            ),
        ),
        ("idna/core.py",),
        "hard",
        ("green-field",),
    ),
    Task(
        "impl-wcwidth-bisearch",
        "wcwidth",
        "wcwidth does not import: its interval-table binary search is missing "
        "(`from .bisearch import bisearch` fails). Implement `bisearch(ucs, "
        "table)` in wcwidth/bisearch.py: given an ordinal and a tuple of "
        "(start, end) inclusive ranges sorted ascending, return 1 if the "
        "ordinal falls inside any range, else 0. It is on the hot path, so use "
        "binary search. Then run the `verify` check.",
        (
            (
                "wcwidth/bisearch.py",
                "def bisearch(ucs: int, table: tuple[tuple[int, int], ...]) -> int:\n"
                '    """\n'
                "    Binary search in interval table.\n"
                "\n"
                "    :param ucs: Ordinal value of unicode character.\n"
                "    :param table: Tuple of starting and ending ranges of ordinal "
                "values, in form of ``((start, end),\n"
                "        ...)``.\n"
                "    :returns: 1 if ordinal value ucs is found within lookup table, else 0.\n"
                '    """\n'
                "    lbound = 0\n"
                "    ubound = len(table) - 1\n"
                "\n"
                "    if ucs < table[0][0] or ucs > table[ubound][1]:\n"
                "        return 0\n"
                "\n"
                "    while ubound >= lbound:\n"
                "        mid = (lbound + ubound) // 2\n"
                "        if ucs > table[mid][1]:\n"
                "            lbound = mid + 1\n"
                "        elif ucs < table[mid][0]:\n"
                "            ubound = mid - 1\n"
                "        else:\n"
                "            return 1\n"
                "\n"
                "    return 0\n",
                "# TODO: bisearch(ucs, table) is missing and must be implemented.\n",
            ),
        ),
        ("wcwidth/bisearch.py",),
        "hard",
        ("green-field",),
    ),
    Task(
        "impl-tomli-cached-tz",
        "tomli",
        "Parsing any TOML datetime with a timezone offset raises NameError: "
        "name 'cached_tz' is not defined — the helper that builds the tzinfo "
        "from the matched offset (sign, hours, minutes) is missing from "
        "src/tomli/_re.py. Implement it and run the `verify` check.",
        (
            (
                "src/tomli/_re.py",
                "# No need to limit cache size. This is only ever called on input\n"
                "# that matched RE_DATETIME, so there is an implicit bound of\n"
                "# 24 (hours) * 60 (minutes) * 2 (offset direction) = 2880.\n"
                "@lru_cache(maxsize=None)\n"
                "def cached_tz(hour_str: str, minute_str: str, sign_str: str) -> timezone:\n"
                '    sign = 1 if sign_str == "+" else -1\n'
                "    return timezone(\n"
                "        timedelta(\n"
                "            hours=sign * int(hour_str),\n"
                "            minutes=sign * int(minute_str),\n"
                "        )\n"
                "    )\n\n\n",
                "",
            ),
        ),
        ("src/tomli/_re.py",),
        "hard",
        ("green-field",),
    ),
    Task(
        "impl-tabulate-padboth",
        "tabulate",
        "Center alignment in tabulate raises NotImplementedError: the body of "
        "`_padboth` (pad a string on both sides to the given display width) was "
        "never written. Implement it consistently with `_padleft` and "
        "`_padright`, and run the `verify` check.",
        (
            (
                "tabulate/__init__.py",
                '    fmt = f"{{0:^{width}s}}"\n    return fmt.format(s)',
                '    raise NotImplementedError("_padboth is not implemented")',
            ),
        ),
        ("tabulate/__init__.py",),
        "medium",
        ("green-field",),
    ),
    # ---- tier 2: two independent bugs in different files -----------------
    Task(
        "multi-jmespath",
        "jmespath",
        "Two independent regressions: (1) the `<` comparison operator treats "
        "equal values as less-than; (2) the `contains()` function returns the "
        "opposite of the correct answer. Find and fix both, then run the "
        "`verify` check.",
        (
            ("jmespath/visitor.py", "        'lt': operator.lt,", "        'lt': operator.le,"),
            (
                "jmespath/functions.py",
                "        return search in subject",
                "        return search not in subject",
            ),
        ),
        ("jmespath/visitor.py", "jmespath/functions.py"),
        "hard",
        ("multi-file",),
    ),
    Task(
        "multi-idna",
        "idna",
        "Two independent regressions: (1) label length validation accepts one "
        "octet too many; (2) codepoint interval ranges are encoded with the "
        "start packed into the wrong bit position, corrupting lookups. Find and "
        "fix both, then run the `verify` check.",
        (
            ("idna/core.py", "    return len(label) <= 63", "    return len(label) <= 64"),
            (
                "idna/intranges.py",
                "    return (start << 32) | end",
                "    return (start << 16) | end",
            ),
        ),
        ("idna/core.py", "idna/intranges.py"),
        "hard",
        ("multi-file",),
    ),
    Task(
        "multi-wcwidth",
        "wcwidth",
        "Two independent regressions: (1) printable ASCII characters report "
        "width 2 instead of 1; (2) the interval-table binary search never "
        "reports a hit, so wide and zero-width lookups all fail. Find and fix "
        "both, then run the `verify` check.",
        (
            (
                "wcwidth/_wcwidth.py",
                "    if 32 <= ucs < 0x7f:\n        return 1",
                "    if 32 <= ucs < 0x7f:\n        return 2",
            ),
            (
                "wcwidth/bisearch.py",
                "        else:\n            return 1",
                "        else:\n            return 0",
            ),
        ),
        ("wcwidth/_wcwidth.py", "wcwidth/bisearch.py"),
        "hard",
        ("multi-file",),
    ),
    # ---- tier 3: issue-style goals on 10k+ line repositories --------------
    # The goal is a user-voice symptom report: it names observable behavior
    # and user-facing feature names, never a file or a function. Locating the
    # defect is the part under measurement; the injection below stays a
    # single surgical edit so the oracle (red-with-bug, green-reverted) and
    # the scope check stay exact. Difficulty is the vagueness of the report
    # times the size of the search space, labelled per task.
    #
    # ---- tier 3: jinja ----------------------------------------------------
    Task(
        "t3-jinja-default-filter",
        "jinja",
        'A user reports: "Since this build, templates using the default filter '
        "render empty output for variables that are not defined at all — "
        "{{ missing|default('n/a') }} prints nothing instead of n/a. "
        "Defined-but-falsy values with the boolean flag still work, so only "
        'the undefined case regressed." Find the cause, fix it, then run the '
        "`verify` check.",
        (
            (
                "src/jinja2/filters.py",
                "    if isinstance(value, Undefined) or (boolean and not value):\n"
                "        return default_value",
                "    if boolean and not value:\n        return default_value",
            ),
        ),
        ("src/jinja2/filters.py",),
        "easy",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-jinja-loop-first",
        "jinja",
        'A user reports: "Inside {% for %} loops the first-iteration flag on '
        "the loop variable is never true anymore, so the header we render "
        "once at the top of each loop has disappeared. The last-iteration "
        'flag still behaves." Find the cause, fix it, then run the `verify` '
        "check.",
        (
            (
                "src/jinja2/runtime.py",
                "        return self.index0 == 0",
                "        return self.index0 == 1",
            ),
        ),
        ("src/jinja2/runtime.py",),
        "easy",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-jinja-truncate-leeway",
        "jinja",
        'A user reports: "The truncate filter now chops strings that are only '
        "a character or two over the limit. It used to leave short overruns "
        "alone unless they exceeded the tolerance margin; truncation of "
        'clearly-too-long strings is still correct." Find the cause, fix it, '
        "then run the `verify` check.",
        (
            (
                "src/jinja2/filters.py",
                "    if len(s) <= length + leeway:\n        return s",
                "    if len(s) <= length - leeway:\n        return s",
            ),
        ),
        ("src/jinja2/filters.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-jinja-trim-blocks",
        "jinja",
        'A user reports: "Template whitespace went weird after upgrading: with '
        "trim_blocks enabled the newline after a block tag is no longer "
        "removed, and with it disabled the newline disappears — exactly "
        'backwards from what the documentation says." Find the cause, fix it, '
        "then run the `verify` check.",
        (
            (
                "src/jinja2/lexer.py",
                '        block_suffix_re = "\\\\n?" if environment.trim_blocks else ""',
                '        block_suffix_re = "" if environment.trim_blocks else "\\\\n?"',
            ),
        ),
        ("src/jinja2/lexer.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-jinja-async-loop-last",
        "jinja",
        'A user reports: "When we render templates asynchronously, the '
        "last-iteration flag on the loop variable never becomes true — the "
        "trailing comma we suppress on the last item now shows up after every "
        "item. Rendering the same template synchronously is correct, so it is "
        'specific to async." Find the cause, fix it, then run the `verify` '
        "check.",
        (
            (
                "src/jinja2/runtime.py",
                "    async def last(self) -> bool:  # type: ignore\n"
                "        return await self._peek_next() is missing",
                "    async def last(self) -> bool:  # type: ignore\n"
                "        return await self._peek_next() is not missing",
            ),
        ),
        ("src/jinja2/runtime.py",),
        "hard",
        ("tier3", "issue-style"),
    ),
    # ---- tier 3: click ----------------------------------------------------
    Task(
        "t3-click-echo-stderr",
        "click",
        'A user reports: "Messages our CLI prints with the error flag are '
        "showing up in standard output instead of standard error, so piping "
        "the command captures diagnostics along with the real output. It used "
        'to keep them apart." Find the cause, fix it, then run the `verify` '
        "check.",
        (
            (
                "src/click/utils.py",
                "        if err:\n            file = _default_text_stderr()",
                "        if err:\n            file = _default_text_stdout()",
            ),
        ),
        ("src/click/utils.py",),
        "easy",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-click-bool-onoff",
        "click",
        'A user reports: "Boolean options stopped accepting on/off: '
        "--feature=on now fails with 'is not a valid boolean', although "
        "true/false, yes/no and 1/0 all still work. Our deploy scripts use "
        'on/off everywhere." Find the cause, fix it, then run the `verify` '
        "check.",
        (
            (
                "src/click/types.py",
                '        "on": True,\n        "off": False,\n',
                "",
            ),
        ),
        ("src/click/types.py",),
        "easy",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-click-range-clamp",
        "click",
        'A user reports: "Integer range options with clamping enabled clamp to '
        "the wrong end: with a 0..10 range, passing -5 comes back as 10 and "
        "passing 100 comes back as 0. Without clamping the range check still "
        'errors correctly." Find the cause, fix it, then run the `verify` '
        "check.",
        (
            (
                "src/click/types.py",
                "        if self.clamp:\n"
                "            if min is not None and lt_min:\n"
                "                return self._clamp(min, 1, self.min_open)\n"
                "\n"
                "            if max is not None and gt_max:\n"
                "                return self._clamp(max, -1, self.max_open)",
                "        if self.clamp:\n"
                "            if min is not None and lt_min:\n"
                "                return self._clamp(max, -1, self.max_open)\n"
                "\n"
                "            if max is not None and gt_max:\n"
                "                return self._clamp(min, 1, self.min_open)",
            ),
        ),
        ("src/click/types.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-click-show-default",
        "click",
        "A user reports: \"--help no longer prints '[default: ...]' for "
        "options that ask for it — the only place a default still shows is "
        "when it was given as an explicit string. Everything else about the "
        'help output looks normal." Find the cause, fix it, then run the '
        "`verify` check.",
        (
            (
                "src/click/core.py",
                "        if show_default_is_str or (\n"
                "            show_default and (default_value not in (None, UNSET))\n"
                "        ):",
                "        if show_default_is_str or (\n"
                "            show_default and (default_value in (None, UNSET))\n"
                "        ):",
            ),
        ),
        ("src/click/core.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-click-nargs-order",
        "click",
        'A user reports: "Positional arguments that take a fixed number of '
        "values receive them reversed — a copy command declared to take SRC "
        "DST as one two-value argument now gets DST first. Options taking "
        'multiple values are unaffected." Find the cause, fix it, then run '
        "the `verify` check.",
        (
            (
                "src/click/parser.py",
                "            # If we're reversed, we're pulling in the arguments in reverse,\n"
                "            # so we need to turn them around.\n"
                "            if spos is not None:\n"
                "                x.reverse()",
                "            # If we're reversed, we're pulling in the arguments in reverse,\n"
                "            # so we need to turn them around.\n"
                "            if spos is None:\n"
                "                x.reverse()",
            ),
        ),
        ("src/click/parser.py",),
        "hard",
        ("tier3", "issue-style"),
    ),
    # ---- tier 3: rich -----------------------------------------------------
    Task(
        "t3-rich-style-bold",
        "rich",
        'A user reports: "Text styled as bold renders faint/dim in the '
        "terminal instead of bold. Every other attribute — italic, "
        "underline, reverse — still comes out right, and bold looks correct "
        'in exported HTML, so it is the terminal escape codes." Find the '
        "cause, fix it, then run the `verify` check.",
        (
            (
                "rich/style.py",
                '    _style_map = {\n        0: "1",',
                '    _style_map = {\n        0: "2",',
            ),
        ),
        ("rich/style.py",),
        "easy",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-rich-padding-pair",
        "rich",
        'A user reports: "Padding given a two-value tuple applies the values '
        "sideways: (1, 4) now pads 4 rows above and below and 1 column left "
        "and right. The docs say two values mean vertical then horizontal, "
        'like CSS. Single values and 4-tuples behave." Find the cause, fix '
        "it, then run the `verify` check.",
        (
            (
                "rich/padding.py",
                "        if len(pad) == 2:\n"
                "            pad_top, pad_right = pad\n"
                "            return (pad_top, pad_right, pad_top, pad_right)",
                "        if len(pad) == 2:\n"
                "            pad_top, pad_right = pad\n"
                "            return (pad_right, pad_top, pad_right, pad_top)",
            ),
        ),
        ("rich/padding.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-rich-progress-percentage",
        "rich",
        'A user reports: "Progress displays are stuck near zero — a task more '
        "than half done shows 0% and a sliver of bar, and even a finished "
        "task barely registers 1%. The completed and total counts themselves "
        'are right." Find the cause, fix it, then run the `verify` check.',
        (
            (
                "rich/progress.py",
                "        completed = (self.completed / self.total) * 100.0",
                "        completed = self.completed / self.total",
            ),
        ),
        ("rich/progress.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-rich-truncate-ellipsis",
        "rich",
        'A user reports: "Text truncated with an ellipsis ends up one cell '
        "too wide, so truncated cells overflow their column and table "
        "borders break on long content. Crop and fold overflow modes are "
        'fine — only ellipsis." Find the cause, fix it, then run the '
        "`verify` check.",
        (
            (
                "rich/text.py",
                '                    self.plain = set_cell_size(self.plain, max_width - 1) + "…"',
                '                    self.plain = set_cell_size(self.plain, max_width) + "…"',
            ),
        ),
        ("rich/text.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-rich-cell-width",
        "rich",
        'A user reports: "Anything containing CJK text or emoji misaligns: '
        "table borders drift right by one cell for every wide character in a "
        "row, and zero-width characters push borders too. ASCII-only content "
        'lines up perfectly." Find the cause, fix it, then run the `verify` '
        "check.",
        (
            (
                "rich/cells.py",
                "        else:\n            return width\n    return 1",
                "        else:\n            return 1\n    return 1",
            ),
        ),
        ("rich/cells.py",),
        "hard",
        ("tier3", "issue-style"),
    ),
    # ---- tier 3: pygments -------------------------------------------------
    Task(
        "t3-pygments-html-linenos",
        "pygments",
        'A user reports: "HTML output with inline line numbers starts '
        "counting at 2 — every displayed number and anchor is one higher "
        "than the actual source line. The table style of line numbering is "
        'not affected." Find the cause, fix it, then run the `verify` check.',
        (
            (
                "pygments/formatters/html.py",
                "        num = self.linenostart\n",
                "        num = self.linenostart + 1\n",
            ),
        ),
        ("pygments/formatters/html.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-pygments-irc-color-pad",
        "pygments",
        'A user reports: "Code snippets our bot posts to IRC render broken '
        "colors whenever the colored text begins with a digit — the color "
        "code swallows it. IRC clients need the color number zero-padded to "
        "two digits, and the output no longer pads it, so a code like 2 "
        'followed by the text 123 reads as color 21." Find the cause, fix '
        "it, then run the `verify` check.",
        (
            (
                "pygments/formatters/irc.py",
                "        add += '\\x03' + str(IRC_COLOR_MAP[color]).zfill(2)",
                "        add += '\\x03' + str(IRC_COLOR_MAP[color])",
            ),
        ),
        ("pygments/formatters/irc.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-pygments-shebang",
        "pygments",
        'A user reports: "Choosing a highlighter by shebang broke: extension-'
        "less scripts that start with #!/usr/bin/env python (or any "
        "interpreter path) are no longer recognized as that language. "
        'Detection by file extension still works." Find the cause, fix it, '
        "then run the `verify` check.",
        (
            (
                "pygments/util.py",
                "            found = [x for x in split_path_re.split(first_line[2:].strip())\n"
                "                     if x and not x.startswith('-')][-1]",
                "            found = [x for x in split_path_re.split(first_line[2:].strip())\n"
                "                     if x and not x.startswith('-')][0]",
            ),
        ),
        ("pygments/util.py",),
        "medium",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-pygments-float-exponent",
        "pygments",
        'A user reports: "In Python code, floats written in scientific '
        "notation with a lowercase e and no decimal point — like 1e100 — "
        "split into a number token followed by a name token. 1E100 and "
        '1.5e100 still highlight as a single float." Find the cause, fix it, '
        "then run the `verify` check.",
        (
            (
                "pygments/lexers/python.py",
                "            (r'\\d(?:_?\\d)*[eE][+-]?\\d(?:_?\\d)*j?', Number.Float),",
                "            (r'\\d(?:_?\\d)*[E][+-]?\\d(?:_?\\d)*j?', Number.Float),",
            ),
        ),
        ("pygments/lexers/python.py",),
        "hard",
        ("tier3", "issue-style"),
    ),
    Task(
        "t3-pygments-token-subtype",
        "pygments",
        'A user reports: "Style rules and filters that target a broad token '
        "category no longer apply to its more specific subtypes — a rule for "
        "String does nothing to String.Double — while, bizarrely, the broad "
        'category now counts as a subtype of its own children." Find the '
        "cause, fix it, then run the `verify` check.",
        (
            (
                "pygments/token.py",
                "    def __contains__(self, val):\n"
                "        return self is val or (\n"
                "            type(val) is self.__class__ and\n"
                "            val[:len(self)] == self\n"
                "        )",
                "    def __contains__(self, val):\n"
                "        return self is val or (\n"
                "            type(val) is self.__class__ and\n"
                "            self[:len(val)] == val\n"
                "        )",
            ),
        ),
        ("pygments/token.py",),
        "hard",
        ("tier3", "issue-style"),
    ),
]
