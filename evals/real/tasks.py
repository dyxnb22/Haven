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
]
