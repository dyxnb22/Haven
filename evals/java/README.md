# Java localization benchmark

Ten read-only localization tasks against a real Spring service, used to decide
whether Haven's grep-based localization holds up at a difficulty where an index
should win. ADR 0023 deferred the LSP tool and pre-registered the evidence that
would overturn the deferral; this is that evidence, gathered at a difficulty
that matters.

## Why this repository

`big-market-ai-platform`, pinned at commit `d6d675d`: 534 Java files, ~32k
lines, 18 Maven modules, Spring dependency injection throughout, and a DDD
layout where a domain interface and its infrastructure implementation live in
different modules under different names.

Tier 3 of the live eval is single-language Python libraries with unique,
grep-friendly names — the regime where `repo.search` is *most* competitive. A
null result there says little about the index question. This corpus is the
opposite regime.

## The measurement

Localization is scored from the tool trace, not the answer text
(`evals/java/score.py`). Grading the final prose would be a keyword probe: an
agent that lists ten candidate files scores like one that knew the answer. The
trace answers the question that matters — *how much work did localization
take?* — and yields a number even for runs that never produce an answer.

The primary metric is **steps to the first read of an answer file**. Secondary:
whether it was found at all, files read, and searches issued.

## Safety

Every task is read-only and `haven run` is read-only by default, so no task can
modify the repository. The benchmark nevertheless runs against a **copy** at
`/tmp/bigmarket-bench`, never the user's working tree at
`/Users/diaoyuxuan/big-market-ai-platform`.

```bash
rm -rf /tmp/bigmarket-bench
cp -R /Users/diaoyuxuan/big-market-ai-platform /tmp/bigmarket-bench
```

## The answer key

Each answer file was opened and confirmed to contain the answer. `naive hits`
is the number of `.java` files (excluding `target/`) that the obvious query
returns — the difficulty of the task, stated before the run.

| Task | Kind | Naive query | Naive hits | Answer file |
|---|---|---|---:|---|
| u1-ratelimit-fallback | unique-name | `RateLimiter` | 8 | `…/starter/ratelimiter/RateLimiterAspect.java` |
| u2-dynamic-table-name | unique-name | `DynamicTableName` | 2 | `…/db/router/plugin/DynamicTableNamePlugin.java` |
| u3-response-http-status | unique-name | `HttpStatus` | 13 | `…/types/web/ResponseHttpStatusMapper.java` |
| i1-strategy-repository-impl | interface-impl | `IStrategyRepository` | 17 | `…/infrastructure/adapter/repository/StrategyRepository.java` |
| i2-activity-repository-impl | interface-impl | `IActivityRepository` | 20 | `…/infrastructure/adapter/repository/ActivityRepository.java` |
| i3-credit-random-award | interface-impl | `IDistributeAward` | 4 | `…/award/service/distribute/impl/UserCreditRandomAward.java` |
| o1-award-record-insert | overloaded | `insert` | 51 | `…/infrastructure/adapter/repository/AwardDispatchSupport.java` |
| o2-weight-rule-chain | overloaded | `logic` | 14 | `…/strategy/service/rule/chain/impl/RuleWeightLogicChain.java` |
| d1-token-revocation-bean | di-wiring | `ITokenRevocationService` | 7 | `…/domain/auth/config/TokenRevocationConfig.java` |
| d2-thread-pool-bean | di-wiring | `ThreadPoolExecutor` | 6 | `…/starter/data/config/ThreadPoolAutoConfiguration.java` |

Two answer files are deliberately not named after the thing being asked for:
`AwardDispatchSupport` performs the award-record insert, and
`TokenRevocationConfig` chooses between two revocation services. Those are the
cases where a name-based search cannot succeed by luck.

## Running it

```bash
export HAVEN_API_KEY_ENV=DEEPSEEK_API_KEY \
       HAVEN_BASE_URL=https://api.deepseek.com/beta HAVEN_MODEL=deepseek-chat
uv run python -m evals.java.run_benchmark               # one run per task
uv run python -m evals.java.score evals/java/events     # writes report.md
```

## Reading the result

Pre-registered in the plan, so the interpretation is fixed before the numbers:

- `interface-impl` and `overloaded` much worse than `unique-name`, with several
  not found: the semantic-localization evidence ADR 0023's gate asked for.
- All kinds comparable: grep localization holds even in Java, and the LSP
  deferral is confirmed at a difficulty that matters.
- Runs dying on the step or token budget before localization is tested: the
  ceiling binds first, which is its own finding and the thing to fix before any
  index.
