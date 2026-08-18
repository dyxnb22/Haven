"""真实 Java 服务上的定位任务及其经验证的答案键。

Tier 3 要求代理在单语言 Python 库中定位，使用 grep 友好的唯一名称——这是
`repo.search` 最有竞争力的场景。本基准提出相同问题，但场景改为索引应当获胜的
情况：Spring 依赖注入、实现位于其他模块且名称不同的接口，以及五十个文件共享的
方法名。

所有任务都是只读的。每个答案文件都已打开并确认包含答案，`naive_hits` 记录明显
查询会返回多少个文件——这个数字就是难度，应与结果一起进入报告。

四种类型是自变量：

- ``unique-name``   独特标识符；grep 应一次跳转就能定位
- ``interface-impl`` 自然查询落在接口上，而实际工作的类位于别处且名称不同
- ``overloaded``    自然查询词被几十个文件共享
- ``di-wiring``     答案是选择 bean 的 ``@Configuration``，因此无论搜索多少次
  *行为*，都无法直接到达它
"""

from __future__ import annotations

from dataclasses import dataclass

#: 基准仓库固定使用的提交。所有任务都是只读的，因此运行不会移动仓库；
#: 固定提交是为了让后续结果保持可比。
REPO_COMMIT = "d6d675d"

#: 基准运行针对的是副本，绝不会操作用户的工作仓库。
REPO_ORIGIN = "/Users/diaoyuxuan/big-market-ai-platform"
REPO_PATH = "/tmp/bigmarket-bench"


@dataclass(frozen=True, slots=True)
class LocalizationTask:
    id: str
    goal: str
    #: 相对于仓库根目录；读取其中任意一个文件都视为找到了答案。
    answer_files: tuple[str, ...]
    kind: str
    #: 人通常会输入的直接搜索词，以及该搜索词返回的文件数量。
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
