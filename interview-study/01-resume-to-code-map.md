# 简历主张与代码证据地图

本文件把简历项目经历拆成十个可验证主题。学习时始终区分三件事：简历说了什么、代码能证明什么、面试中不能扩大成什么。

## 0. 总体掌握标准

| 层级 | 能力 | 示例 |
| --- | --- | --- |
| L1 定位 | 2 分钟内找到入口和关键实现 | 找到 `RaffleApplicationService.executeDraw` |
| L2 走通 | 能讲正常主链路和状态变化 | 参与订单 → 选奖 → 中奖记录 → 发奖任务 |
| L3 失败 | 能解释超时、崩溃、重复请求 | 中奖记录已落库后库存确认失败为什么不能释放库存 |
| L4 取舍 | 能比较方案并说明边界 | Outbox 提供最终一致性，不是 Exactly Once |

达到 L1 只能说明“看过代码”；面试前，每条简历主张至少达到 L3，Big-Market 的库存和积分发放应达到 L4。

---

# 一、Big-Market

项目根目录：`/Users/diaoyuxuan/big-market-ai-platform`

## 1. 业务流程与模块划分

### 简历主张

按业务边界拆分活动、抽奖策略、奖品发放与积分账户；抽奖服务串联订单、次数扣减、选奖、中奖记录和发奖任务，消息任务服务负责异步处理，账户服务负责积分入账。

### 代码入口与证据

| 证据 | 代码位置 | 要确认的内容 |
| --- | --- | --- |
| HTTP 入口 | `big-market-trigger/.../http/RaffleActivityController.java` | Token 用户如何进入抽奖用例 |
| Trigger 编排 | `big-market-trigger/.../application/RaffleDrawApplicationService.java` | DTO 与领域请求如何转换 |
| 核心用例 | `big-market-domain/.../activity/application/RaffleApplicationService.java` | 创建参与单、策略出奖、保存中奖记录、库存确认的顺序 |
| 跨域端口 | `domain/activity/adapter/port/IStrategyDecisionPort.java`、`IAwardFulfillmentPort.java`、`IActivityAccountPort.java` | 活动域依赖接口而不是具体服务 |
| 本地适配 | `infrastructure/adapter/port/LocalStrategyDecisionPort.java`、`LocalAwardFulfillmentPort.java`、`LocalActivityAccountPort.java` | 当前实现如何委托策略、奖品和活动账户能力 |
| 异步边界 | `SendAwardConsumer.java`、`DispatchCreditAwardTaskJob.java` | Message-Job 为什么拥有消费和补偿职责 |
| 账户写入 | `AccountCreditServiceRPC.java`、`AccountCreditApplicationService.java` | Account 如何接收幂等业务号并入账 |

### 必须会解释

1. `RaffleApplicationService` 是一次抽奖用例的编排器，不负责实现所有规则。
2. `createOrder` 内部完成参与订单复用或创建；新建时才消费额度。
3. 策略域返回候选奖及可能的库存预占结果。
4. 奖品域保存中奖记录和第一层消息任务。
5. Message-Job 异步分发奖品；积分奖品还会产生第二层积分发放任务。
6. Account 只负责积分账户与流水，不负责决定用户抽到什么。

### 面试追问

- 为什么活动域不直接注入 `DefaultRaffleStrategy`？
- 哪些操作处于同一个本地事务，哪些跨越了进程或服务？
- Market、Message-Job、Account 任一服务停机时，业务分别停在哪个持久化状态？

### 表述边界

可以说当前仓库通过端口和适配器明确了业务依赖方向。不要只因为包名分层就说“完整落地了 DDD”，也不要把本地调用路径说成所有能力都已远程拆分。

## 2. 概率装配与选奖

### 简历主张

活动启动时预生成概率结构并写入 Redis；规模不超过阈值时用随机下标和 Hash 定位奖品，超过阈值时用累计概率区间控制缓存占用。

### 代码入口与证据

| 阶段 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 装配入口 | `domain/strategy/service/armory/AbstractStrategyAlgorithm.java` | 装配默认概率表、权重子集表和奖品库存 |
| 算法选择 | `domain/strategy/service/armory/StrategyArmoryDispatch.java` | 概率范围与固定阈值 10000 比较，并缓存算法名 |
| 查找表 | `domain/strategy/service/armory/algorithm/impl/O1Algorithm.java` | 奖品按概率占位、打乱、写为索引到奖品的映射 |
| 累计区间 | `domain/strategy/service/armory/algorithm/impl/OLogNAlgorithm.java` | 保存闭区间；按奖品数选择遍历、二分或分段并行查找 |
| Redis 适配 | `infrastructure/adapter/repository/StrategyAwardCacheSupport.java` | 概率范围、查找表和库存如何存取 |

### 必须会解释

- 这里的 O(1) 指抽奖阶段的随机下标加 Hash 查询，不代表整个装配过程是 O(1)。
- 阈值比较的是离散概率范围，不是用户量、QPS 或奖品库存。
- 查找表用更多缓存空间换取简单的热点查询；累计区间减少高精度概率下的表膨胀。
- 权重规则不是临时修改概率，而是为允许的奖品子集提前装配另一张表。
- 10000 是代码中的工程阈值，不是压测得到的通用最佳值。

### 面试追问

- 0.001% 的最小概率会对查找表造成什么影响？
- 为什么随机下标范围和数组/Hash 下标边界容易出现偏差？
- `OLogNAlgorithm` 在奖品数很少时为什么直接遍历？
- 如果概率之和不为 1，当前实现会出现什么问题，应该在哪一层校验？

### 表述边界

可以描述算法结构和复杂度取舍。没有当前生产压测证据时，不说“显著降低 RT”“支撑某个 QPS”或“保证绝对公平”。

## 3. 抽奖规则

### 简历主张

抽奖前用责任链处理黑名单和累计积分分段，选奖后用决策树检查解锁次数和库存，不满足时转为备用奖品；规则节点独立封装。

### 代码入口与证据

| 阶段 | 代码位置 | 要观察的内容 |
| --- | --- | --- |
| 总入口 | `domain/strategy/service/AbstractRaffleStrategy.java` | 责任链先执行，规则树后执行 |
| 打开责任链 | `domain/strategy/service/raffle/DefaultRaffleStrategy.java` | 根据策略 ID 获取链并执行 |
| 责任链工厂 | `domain/strategy/service/rule/chain/factory/DefaultChainFactory.java` | 配置节点顺序、缓存链、尾部默认节点 |
| 链节点 | `.../rule/chain/impl/BlackListLogicChain.java`、`RuleWeightLogicChain.java`、`DefaultLogicChain.java` | 接管与继续传递的条件 |
| 规则树引擎 | `.../rule/tree/factory/engine/impl/DecisionTreeEngine.java` | 节点返回结果如何选择下一条边 |
| 树节点 | `.../rule/tree/impl/RuleLockLogicTreeNode.java`、`RuleStockLogicTreeNode.java`、`RuleLuckAwardLogicTreeNode.java` | 次数、库存和备用奖品的职责 |

### 必须会解释

- 责任链解决“从哪个奖品范围选”，适合有顺序的前置筛选。
- 决策树解决“候选奖是否能发”，需要依据判断结果走不同分支。
- 黑名单或权重节点接管候选奖后，仍需进入规则树；接管不等于库存已经扣减。
- 链节点使用 Prototype，是因为节点持有可变的 `next` 引用，单例会让不同策略的链互相污染。
- 规则独立封装降低了新增判断对主流程的改动，但配置缓存也带来变更失效问题。

### 面试追问

- 为什么不用一棵树同时完成黑名单、权重和库存？
- `ALLOW` 与 `TAKE_OVER` 在不同节点中的业务语义是什么？
- 修改数据库规则后为什么可能没有立即生效？
- 备用奖品是否也一定扣库存？从代码如何判断？

### 表述边界

使用“责任链”和“决策树”这两个通用名称即可，不把它描述为通用规则平台。当前实现服务于抽奖规则配置，不等同于 Drools 一类完整规则系统。

## 4. 库存一致性

### 简历主张

使用 Redis Lua 原子预占库存，以抽奖订单号防止重复扣减，MySQL 扣减记录作为持久化依据；中奖记录保存失败时释放预占，成功后确认并异步更新数据库，中断后由任务恢复。

### 代码入口与证据

| 阶段 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 触发预占 | `domain/strategy/service/rule/tree/impl/RuleStockLogicTreeNode.java` | 库存节点用订单号作为 `reservationId` |
| Redis 与持久化记录 | `infrastructure/adapter/repository/StrategyAwardCacheSupport.java::reserveStock` | 先建立持久化预占记录，再用 Lua 完成去重、DECR 和锁位 |
| 成功确认 | `RaffleApplicationService.java` | 中奖记录落库后才确认预占并进入延迟队列 |
| 失败释放 | `RaffleApplicationService.java`、`StrategyAwardCacheSupport.java::releaseReservation` | 仅在中奖记录未落库时恢复 Redis 库存 |
| 异步落库 | `UpdateAwardStockJob.java`、`StrategyAwardCacheSupport.java::updateStrategyAwardStockOnce` | MySQL 扣减与持久化记录在同一事务内完成 |
| 中断补偿 | `StrategyAwardStockConfirmJob.java` | 中奖已落库但确认入队失败时补做确认 |

### 必须会解释

库存过程不是简单的“Redis DECR 后异步减 MySQL”，而是三阶段：

```text
reserve：以订单号建立预占事实并原子扣 Redis
  → award record 保存成功
confirm：进入异步落库路径
  → MySQL 库存和扣减记录同事务更新
```

若中奖记录保存失败，才走 `release`。若中奖记录已成功但 `confirm` 失败，不能释放，否则用户已经有中奖事实却把库存还回去了；此时应保存补偿任务继续确认。

Redis 中的快速去重键不能单独证明数据库扣减已经完成。代码在快速键存在时仍检查 MySQL 记录，用来覆盖“Redis 标记已写、数据库事务未完成”的崩溃窗口。

### 面试追问

- 为什么持久化预占记录要在 Lua 扣减前建立？
- 进程在 Redis 扣减后、记录 `lockSurplus` 前退出会怎样恢复？
- 为什么释放也需要按 `reservationId` 幂等？
- 为什么不能只依赖 Redis `SETNX` 判断 MySQL 已扣减？
- 这套实现能否据此声称永不超卖？还缺少哪些生产条件和验证？

### 表述边界

可以说代码通过原子预占、持久化记录、幂等落库和补偿任务覆盖关键崩溃窗口。不要声称生产环境零超卖、高并发容量或 Redis/MySQL 强一致。

## 5. 积分发放

### 简历主张

条件更新中奖记录并同事务写入待发放任务；后台任务调用积分账户服务，使用中奖订单号和业务流水号去重；远程结果不确定时保留任务并对账。

### 代码入口与证据

| 阶段 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 第一层 Outbox | `domain/award/service/AwardService.java::saveUserAwardRecord`、`AwardDispatchSupport.java` | 中奖记录、`task` 与参与订单状态更新的事务边界 |
| MQ 消费 | `trigger/listener/SendAwardConsumer.java` | 重复消费只对明确的重复业务码按成功处理 |
| 积分奖品分发 | `domain/award/service/distribute/impl/UserCreditRandomAward.java` | 根据配置生成积分额度并构造发奖聚合 |
| 第二层 Outbox | `infrastructure/adapter/repository/AwardCreditGrantSupport.java` | CAS 更新中奖记录并写 `credit_award_task` 的同一事务 |
| 定时派发 | `message-job-service/.../DispatchCreditAwardTaskJob.java` | 先调用 Account，再标记任务派发完成 |
| 远程结果分类 | `message-job-service/.../AccountRemoteCreditWriteAdapter.java` | SUCCESS、REJECTED、UNKNOWN 的不同处理 |
| 对账恢复 | `PendingRemoteWriteSupport.java`、`RemoteWriteReconcileJob.java` | UNKNOWN 保存原请求和业务号，后续探测或重试 |
| 最终入账 | `AccountCreditApplicationService.java`、`CreditAdjustService.java` | `outBusinessNo` 对账户流水做幂等保护 |

### 必须会解释

- 第一层 Outbox 保证“中奖事实”和“需要发奖”一起落库。
- 积分奖品消费后，CAS 将中奖记录从待发放推进，并写第二层 `credit_award_task`。
- 后台任务先远程入账再更新本地任务，因此两步之间崩溃会重复调用；Account 必须按 `awardOrderId/outBusinessNo` 去重。
- RPC 超时只代表调用方没有拿到结果，不能直接判定账户未入账。
- `award_state` 表示本地发奖流程已接管，不单独证明积分账户已经到账；最终要关联中奖订单、积分发放任务和账户流水。

### 面试追问

- 为什么需要两层 Outbox，而不是中奖后直接调用 Account？
- 如果 Account 成功入账，但 `credit_award_task` 更新前进程崩溃，会不会重复加积分？
- 为什么 UNKNOWN 不能与明确拒绝使用同一种处理？
- 唯一键、Redisson 锁和 CAS 各解决什么问题，是否可以互相替代？
- Outbox 能提供什么保证，不能提供什么保证？

### 表述边界

应说“本地事务 + 幂等消费 + 重试/对账实现最终一致性”。不要说分布式事务、Exactly Once，也不要把本地中奖记录状态直接解释为最终账户到账状态。

---

# 二、Haven

项目根目录：`/Users/diaoyuxuan/Haven`

## 6. 运行控制

### 简历主张

实现可循环调用工具的本地编码助手、流式模型客户端、TUI/CLI、会话恢复，并通过步骤、工具、时间、Token 和成本预算限制运行。

### 代码入口与证据

| 证据 | 代码位置 | 要确认的内容 |
| --- | --- | --- |
| CLI/TUI 入口 | `src/haven/interfaces/cli.py` | 无子命令启动 TUI，`run` 启动无头运行 |
| 组合根 | `src/haven/bootstrap.py::build_services` | 端口与具体适配器如何组装 |
| 运行循环 | `src/haven/application/run_service.py::RunService._drive` | Model → Tool → Observation → Model 的循环和停止位置 |
| 上下文构造 | `src/haven/application/context_builder.py` | transcript、plan、预算如何形成单轮请求 |
| 状态 | `src/haven/application/state.py` | RunContext 保存哪些可信运行状态 |
| 预算 | `src/haven/domain/budget.py` | 步骤、工具次数、墙上时间、Token 和成本限制 |
| 模型适配 | `src/haven/adapters/providers/openai_compatible.py` | 流式响应如何转换成内部 ModelResult |

### 必须会解释

- State 是程序掌握的运行事实；Context 是某一轮实际发给模型的内容；ModelResult 是这一轮模型返回；Trace 是持久化事件历史。
- 每轮先检查预算，再构造上下文、调用模型、执行工具或判断候选最终答案。
- 模型返回文字不直接决定成功，最终还要通过确定性完成判断。
- `RunService` 当前按单实例单活跃运行设计，不是多代理并发调度器。

### 表述边界

这是本地单工作区 Coding Agent。不要描述成云端托管平台、多代理系统或通用工作流引擎。

## 7. 工具执行流程

### 简历主张

模型只能提出工具请求；程序完成注册检查、参数校验、事实收集、权限判断和必要审批，再生成执行凭证，执行器不直接接收未经验证的模型参数。

### 代码入口与证据

| 阶段 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 单一入口 | `src/haven/application/tool_pipeline.py::ToolPipeline.execute` | 七个编号阶段构成唯一工具通道 |
| 注册和 Schema | `src/haven/application/registry.py`、`src/haven/contracts/tools.py` | 未注册工具和额外/错误参数被拒绝 |
| 事实收集 | `src/haven/application/tool_facts.py` | 工作区、保护路径、前像、配方和沙箱事实由程序产生 |
| 权限策略 | `src/haven/domain/policy.py::evaluate_policy` | 纯函数根据事实返回 allow / ask / deny |
| 执行凭证 | `src/haven/domain/ticket.py` | 绑定工具、版本、规范化参数、工作区、前像和审批标识 |
| 实际执行 | `src/haven/application/tool_execution.py`、`tool_mutations.py`、`tool_processes.py` | 各工具只消费校验后的内部对象和票据摘要 |

### 必须会解释

- 模型 JSON 是不可信提议，不能直接作为 I/O 参数。
- `DENY` 是硬边界，用户审批只能处理 `ASK`，不能覆盖路径越界、保护路径或缺少沙箱。
- 执行凭证是进程内部的授权绑定，不应夸大为外部可验证的加密令牌。
- 工具执行错误被转换为结构化结果返回循环，单次错误不应直接让整个 Agent 进程崩溃。

### 表述边界

可以说执行器与原始模型提议隔离。不要说模型“绝对无法绕过一切攻击”，也不要把摘要绑定说成硬件级或跨进程加密信任。

## 8. 审批与文件一致性

### 简历主张

审批绑定工具版本、规范化参数、工作区、文件修改前摘要和变更预览；条件更新确保只消费一次，执行前再次比较文件状态。

### 代码入口与证据

| 阶段 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 摘要字段 | `src/haven/domain/approval.py::compute_approval_digest` | workspace、tool、version、args、preimage、preview 六类输入 |
| 审批协调 | `src/haven/application/approval_coordinator.py` | 记录请求、等待决定、消费审批 |
| 单次消费 | `src/haven/adapters/sqlite_session.py`、`tests/contract/test_session_store.py` | 条件更新阻止重复或错误摘要消费 |
| TOCTOU 复核 | `src/haven/application/tool_pipeline.py` | 用户确认后、执行前重新读取前像；多文件补丁逐项检查 |
| 原子写入 | `src/haven/adapters/workspace_editor.py`、`workspace_fs.py` | 实际文件变更如何落盘并记录前后摘要 |

### 必须会解释

- 审批授权的是一项精确操作，不是“接下来随便写这个文件”。
- 若参数、预览或原文件发生变化，之前的审批不再匹配。
- 单次消费解决审批重放，TOCTOU 复核解决审批后文件被修改；两者解决的问题不同。
- `repo.apply_patch` 可以一次审批多个文件，但会绑定每个文件的前像，任一过期则整个补丁失败关闭。

### 表述边界

“确保实际操作与确认内容一致”应理解为当前工具协议和摘要覆盖范围内的一致。不要扩大成对工作区所有并发变化都具有事务隔离。

## 9. 完成判断与进程隔离

### 简历主张

不以模型自报完成为准；最后一次修改后必须有非空差异和通过的注册检查，并对新增内容做固定规则检查；命令在 Seatbelt/Landlock 下运行。

### 代码入口与证据

| 证据 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 完成门禁 | `src/haven/domain/evidence.py::evaluate_evidence_gate` | 无写入可直接结束；有写入要求最后写入之后的 diff 和 check |
| 固定审查 | `src/haven/domain/review.py` | 只检查新增行中的明显凭据、冲突标记、调试语句及大面积删除 |
| 运行收口 | `src/haven/application/run_service.py`、`run_persistence.py` | 模型最终文字如何交给 Evidence Gate 决定状态 |
| 进程执行 | `src/haven/application/tool_processes.py`、`src/haven/adapters/process_executor.py` | `repo.exec` 与 `repo.check` 的区别及执行记录 |
| 沙箱 | `src/haven/adapters/sandbox/seatbelt.py`、`landlock.py` | 不同平台如何限制路径、网络和子进程能力 |

### 必须会解释

- “检查通过”必须发生在最后一次写入之后；旧的绿色测试不能证明新的修改。
- `repo.exec` 的退出码只是观察结果，只有注册的 `repo.check` 才是完成证据。
- 没有检查配方却发生写入时，门禁是不可满足的终止状态，不应让模型无限重试。
- 固定规则审查是低误报启发式，不是完整秘密扫描、静态分析或安全审计。
- Seatbelt/Landlock 是操作系统进程限制，不是虚拟机或完整容器隔离。

### 表述边界

不要说“保证代码正确”或“保证没有安全漏洞”。准确说法是：程序要求可归因的差异、修改后的注册检查以及有限的确定性审查，减少模型凭文字宣告成功。

## 10. 中断恢复

### 简历主张

SQLite 保存检查点、追加事件和副作用执行记录；重启后比较文件修改前后摘要，分类为未执行、已完成或无法确定，无法确定时停止自动重试。

### 代码入口与证据

| 证据 | 代码位置 | 关键事实 |
| --- | --- | --- |
| 契约 | `src/haven/contracts/events.py`、`checkpoint.py` | 事件与快照分别保存什么 |
| 持久化 | `src/haven/adapters/sqlite_session.py` | runs、events、checkpoints、approvals、executions、artifacts |
| 写入时机 | `src/haven/application/run_persistence.py`、`tool_mutations.py`、`tool_processes.py` | STARTED、CONFIRMED、FAILED、EFFECT_UNKNOWN 如何产生 |
| 恢复检查 | `src/haven/application/recovery_service.py::inspect` | 工作区身份、构件、执行记录和当前文件摘要如何核对 |
| 人工处理 | `src/haven/interfaces/cli_support/recovery_commands.py` | `resume`、`reconcile`、`rewind` 的区别 |

### 必须会解释

- 事件日志用于审计和重放视图，检查点用于快速恢复状态，执行记录专门保存副作用是否开始/完成；三者不能互相替代。
- 编辑后当前文件等于 preimage，说明操作未执行；等于 postimage，说明操作已完成；两者都不等于则无法证明。
- 进程命令没有可用的文件前后摘要来证明是否运行，因此中断后保持 UNKNOWN，需要人工判断。
- 自动重放未知副作用可能重复删除、重复命令或覆盖外部修改，所以系统选择停止。
- `rewind` 是受当前文件摘要保护的补偿操作，不是 `git reset` 式强制覆盖。

### 表述边界

这是本地单仓库的崩溃恢复机制。不要描述为分布式共识、事件溯源业务系统或跨节点工作流恢复。

---

# 三、跨项目复习索引

| 通用后端问题 | Big-Market 证据 | Haven 证据 |
| --- | --- | --- |
| 如何限制副作用重复发生 | 订单号、消息号、业务流水号、CAS、唯一键 | 单次审批、执行凭证、执行记录 |
| 如何处理不确定结果 | 待对账任务、账户流水探测、幂等重试 | EFFECT_UNKNOWN、摘要分类、人工 reconcile |
| 如何定义完成 | 业务记录、任务状态与账户流水闭环 | 最后写入后的 diff、check 和固定审查 |
| 如何保存恢复依据 | Outbox、库存扣减记录、业务订单 | 事件日志、检查点、executions、artifacts |
| 如何隔离业务与外部实现 | Domain Port / Repository 与 Adapter | ports 与 adapters，bootstrap 组装 |

跨项目比较只用于展示工程思考。Big-Market 解决分布式业务一致性，Haven 解决本地 Agent 副作用控制；两者的对象、保证和风险模型不同。

