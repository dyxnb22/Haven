"""Localization tasks over a real Java service, and their verified answer key.

Tier 3 asks the agent to localize inside single-language Python libraries with
grep-friendly unique names — the regime where `repo.search` is most
competitive. This benchmark asks the same question where an index should win:
Spring dependency injection, interfaces whose implementation lives in another
module under another name, and method names shared by fifty files.

Every task is read-only. Each answer file was opened and confirmed to contain
the answer, and `naive_hits` records how many files the obvious query returns —
that number is the difficulty, and it belongs in the report next to the result.

The four kinds are the independent variable:

- ``unique-name``   a distinctive identifier; grep should localize in one hop
- ``interface-impl`` the natural query lands on the interface, and the class
  that does the work is elsewhere with a different name
- ``overloaded``    the natural query term is shared by dozens of files
- ``di-wiring``     the answer is a ``@Configuration`` that chooses a bean, so
  no amount of searching for the *behaviour* reaches it directly
"""

from __future__ import annotations

from dataclasses import dataclass

#: Pinned commit of the benchmark repository. Every task is read-only, so a
#: run cannot move it; the pin exists so a later result is comparable.
REPO_COMMIT = "d6d675d"

#: The benchmark runs against a copy, never the user's working repository.
REPO_ORIGIN = "/Users/diaoyuxuan/big-market-ai-platform"
REPO_PATH = "/tmp/bigmarket-bench"


@dataclass(frozen=True, slots=True)
class LocalizationTask:
    id: str
    goal: str
    #: Repo-relative; reading any one of them counts as having found it.
    answer_files: tuple[str, ...]
    kind: str
    #: The obvious search a person would type, and how many files it returns.
    naive_query: str
    naive_hits: int


_INFRA = "big-market-infrastructure/src/main/java/com/dyx/market/infrastructure"
_DOMAIN = "big-market-domain/src/main/java/com/dyx/market/domain"

TASKS: tuple[LocalizationTask, ...] = (
    LocalizationTask(
        id="u1-ratelimit-fallback",
        goal=(
            "In this Java repository, a method can be annotated so that callers who exceed "
            "a rate limit are diverted to a fallback method instead of getting an error. "
            "Find the file that implements that interception and explain how the fallback "
            "is invoked. Do not modify anything."
        ),
        answer_files=(
            "big-market-starter-ratelimiter/src/main/java/com/dyx/market/starter/"
            "ratelimiter/RateLimiterAspect.java",
        ),
        kind="unique-name",
        naive_query="RateLimiter",
        naive_hits=8,
    ),
    LocalizationTask(
        id="u2-dynamic-table-name",
        goal=(
            "This repository shards some database tables. Find the file that rewrites the "
            "physical table name inside a SQL statement at runtime so a sharded table is "
            "addressed correctly, and explain how it picks the suffix. Do not modify anything."
        ),
        answer_files=(
            "big-market-starter-db-router/src/main/java/com/dyx/market/middleware/db/"
            "router/plugin/DynamicTableNamePlugin.java",
        ),
        kind="unique-name",
        naive_query="DynamicTableName",
        naive_hits=2,
    ),
    LocalizationTask(
        id="u3-response-http-status",
        goal=(
            "The API returns a business result code in its response body. Find the file that "
            "decides which HTTP status code accompanies a given business code, and list the "
            "mappings it defines. Do not modify anything."
        ),
        answer_files=(
            "big-market-types/src/main/java/com/dyx/market/types/web/ResponseHttpStatusMapper.java",
        ),
        kind="unique-name",
        naive_query="HttpStatus",
        naive_hits=13,
    ),
    LocalizationTask(
        id="i1-strategy-repository-impl",
        goal=(
            "The raffle strategy domain reads and writes its data through a repository "
            "interface. Find the concrete class that actually implements that interface "
            "against the database and cache, and name the file. Do not modify anything."
        ),
        answer_files=(f"{_INFRA}/adapter/repository/StrategyRepository.java",),
        kind="interface-impl",
        naive_query="IStrategyRepository",
        naive_hits=17,
    ),
    LocalizationTask(
        id="i2-activity-repository-impl",
        goal=(
            "The activity domain reads and writes its data through a repository interface. "
            "Find the concrete class that actually implements it against the database, and "
            "name the file. Do not modify anything."
        ),
        answer_files=(f"{_INFRA}/adapter/repository/ActivityRepository.java",),
        kind="interface-impl",
        naive_query="IActivityRepository",
        naive_hits=20,
    ),
    LocalizationTask(
        id="i3-credit-random-award",
        goal=(
            "Awards are handed out through a distribution interface with several "
            "implementations. Find the one that grants the user a random amount of credit, "
            "and explain how the random amount is bounded. Do not modify anything."
        ),
        answer_files=(f"{_DOMAIN}/award/service/distribute/impl/UserCreditRandomAward.java",),
        kind="interface-impl",
        naive_query="IDistributeAward",
        naive_hits=4,
    ),
    LocalizationTask(
        id="o1-award-record-insert",
        goal=(
            "When a user wins an award, a row recording it is written to the database. Find "
            "the file where that insert actually happens — not the interface or the DAO "
            "declaration, the code that calls it. Do not modify anything."
        ),
        answer_files=(f"{_INFRA}/adapter/repository/AwardDispatchSupport.java",),
        kind="overloaded",
        naive_query="insert",
        naive_hits=51,
    ),
    LocalizationTask(
        id="o2-weight-rule-chain",
        goal=(
            "During a raffle, a chain of rule nodes runs before an award is chosen. One node "
            "applies a configured weight rule so that users past a spend threshold draw from "
            "a different award pool. Find that node's class file. Do not modify anything."
        ),
        answer_files=(f"{_DOMAIN}/strategy/service/rule/chain/impl/RuleWeightLogicChain.java",),
        kind="overloaded",
        naive_query="logic",
        naive_hits=14,
    ),
    LocalizationTask(
        id="d1-token-revocation-bean",
        goal=(
            "Token revocation has more than one implementation in this codebase. Find the "
            "file that decides which implementation is actually wired in when the "
            "application starts, and state what switches it. Do not modify anything."
        ),
        answer_files=(f"{_DOMAIN}/auth/config/TokenRevocationConfig.java",),
        kind="di-wiring",
        naive_query="ITokenRevocationService",
        naive_hits=7,
    ),
    LocalizationTask(
        id="d2-thread-pool-bean",
        goal=(
            "Asynchronous work in this project is submitted to a shared thread pool that is "
            "injected as a Spring bean. Find the file that constructs that bean and state "
            "which configuration prefix tunes it. Do not modify anything."
        ),
        answer_files=(
            "big-market-starter-data/src/main/java/com/dyx/market/starter/data/config/"
            "ThreadPoolAutoConfiguration.java",
        ),
        kind="di-wiring",
        naive_query="ThreadPoolExecutor",
        naive_hits=6,
    ),
)


def by_id(task_id: str) -> LocalizationTask:
    for task in TASKS:
        if task.id == task_id:
            return task
    raise KeyError(task_id)
