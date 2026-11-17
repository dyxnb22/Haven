# 14 天学习计划

这是一轮完整复习，不是一次性冲刺。每天建议 2～3 小时：代码阅读 90 分钟、基础知识回补 45 分钟、口述和复盘 30 分钟。若时间不足，保留代码主链路和口述，减少扩展阅读。

## 总体安排

| 阶段 | 天数 | 目标 |
| --- | ---: | --- |
| 建立全局图 | 1～2 | 知道两个项目解决什么问题、模块如何协作 |
| Big-Market 深挖 | 3～8 | 掌握抽奖、规则、库存、发奖和积分闭环 |
| Haven 深挖 | 9～11 | 掌握运行循环、工具通道、完成判断和恢复 |
| 面试整合 | 12～14 | 将代码理解转换成项目介绍和追问回答 |

## Day 1：简历主张盘点

任务：

- 逐字阅读两个项目的十条简历要点。
- 使用[简历主张与代码证据地图](01-resume-to-code-map.md)，为每条要点标注“会 / 模糊 / 不会”。
- 不看项目文档，分别写一段 100 字项目介绍。
- 建立术语清单，只保留通用概念：本地事务、幂等、条件更新、消息任务、摘要、审批、沙箱、恢复。

当天输出：两个项目的 30 秒介绍；十条要点的掌握度表。

## Day 2：系统边界与模块图

Big-Market：画出 Market、Message-Job、Account 以及 Redis、MySQL、RabbitMQ、XXL-Job 的关系。

Haven：画出 interfaces、bootstrap、application、domain、ports、adapters、contracts 的依赖方向。

基础复习：DDD 分层、依赖倒置、应用服务与领域服务的区别、端口与适配器。

当天输出：两张不超过 12 个节点的架构图；解释为什么核心规则不直接依赖中间件。

## Day 3：Big-Market 单次抽奖主链路

从 `RaffleActivityController` 跟到 `RaffleApplicationService.executeDraw`，再依次阅读参与订单、策略决策、中奖记录和库存确认。

基础复习：Spring Controller 到 Service 的调用、事务传播、聚合与事务边界。

当天输出：一次抽奖的顺序图；说明订单复用为什么不会重复扣次数。

## Day 4：概率装配与两种选奖算法

阅读 `AbstractStrategyAlgorithm`、`StrategyArmoryDispatch`、`O1Algorithm` 和 `OLogNAlgorithm`。

基础复习：Hash 查询、累计区间、二分查找、空间复杂度、随机数边界、BigDecimal 精度。

当天输出：用一组 50%、30%、20% 的奖品手算两种表；说明阈值 10000 控制的是什么，而不声称它是性能测试结论。

## Day 5：责任链与决策树

阅读责任链工厂、黑名单、权重、默认节点以及规则树引擎、次数锁、库存和备用奖品节点。

基础复习：责任链、策略、模板方法、组合/树遍历、Spring Prototype Bean。

当天输出：回答“为什么不用一条责任链完成所有规则”；解释接管候选奖后为什么仍要检查库存。

## Day 6：库存预占、确认和释放

从 `RuleStockLogicTreeNode` 跟到 `StrategyAwardCacheSupport`，重点阅读 Lua 脚本、持久化扣减记录、延迟队列、确认、释放和恢复。

基础复习：Redis 命令原子性、Lua、SETNX、Redisson 延迟队列、数据库唯一键、条件更新。

当天输出：库存 `reserved / applied / released` 状态图；解释“Redis 快速路径”和“MySQL 持久化事实”的区别。

## Day 7：中奖记录与第一层消息任务

阅读 `AwardService.saveUserAwardRecord`、`AwardDispatchSupport`、发送任务 Job 和 `SendAwardConsumer`。

基础复习：本地消息表 Outbox、消息重复投递、消费者幂等、先写库还是先发消息。

当天输出：画出中奖记录与消息任务同事务的边界；列出先发 MQ 再写库的两个失败窗口。

## Day 8：积分发放与远程不确定结果

阅读积分奖品分发、`AwardCreditGrantSupport`、`credit_award_task`、`DispatchCreditAwardTaskJob`、远程账户适配器和待对账任务。

基础复习：RPC 超时不等于失败、业务幂等号、重试、对账、最终一致性、Exactly Once 为什么难以成立。

当天输出：从 `award_order_id` 跟到积分流水；解释“中奖记录完成”为什么不等于“积分已经到账”。

## Day 9：Haven 运行循环

从 CLI、`bootstrap.build_services` 跟到 `RunService._drive`，区分 State、Context、ModelResult 和 Trace。

基础复习：Python `asyncio`、端口与适配器、组合根、有限状态机、预算控制。

当天输出：模型调用和工具调用交替运行的流程图；列出五类预算以及触发停止的位置。

## Day 10：Haven 工具执行与审批

阅读 `ToolPipeline.execute`、`evaluate_policy`、`ApprovalCoordinator`、审批摘要和执行凭证。

基础复习：严格 Schema、规范化参数、摘要、TOCTOU、失败关闭、最小权限。

当天输出：八步工具通道；说明用户同意为什么仍不能覆盖越界路径等硬拒绝。

## Day 11：Haven 完成判断、沙箱与恢复

阅读 `evaluate_evidence_gate`、`review_diff`、进程执行适配器、Seatbelt/Landlock 以及 `RecoveryService.inspect`。

基础复习：事件日志与快照、前像/后像、崩溃一致性、操作系统进程隔离边界。

当天输出：Evidence Gate 真值表；编辑、创建、删除、移动和进程调用的恢复分类表。

## Day 12：跨项目比较

比较两类问题：

| Big-Market | Haven |
| --- | --- |
| 防止业务副作用重复发生 | 防止未经验证的工具副作用发生 |
| 订单号、消息号作为幂等键 | 参数、工作区、文件前像组成摘要 |
| Outbox、库存扣减记录、对账任务 | 事件日志、执行记录、检查点 |
| UNKNOWN 时保留任务并对账 | EFFECT_UNKNOWN 时停止自动重放并人工处理 |

当天输出：一段 2 分钟比较；明确两者场景不同，不能把 Haven 说成分布式事务系统，也不能把 Big-Market 说成安全沙箱。

## Day 13：模拟面试

进行三轮录音：

1. 30 秒项目概览；
2. 2 分钟核心设计；
3. 10 分钟深挖一个失败场景。

使用[面试复习与回答方法](04-interview-review-guide.md)中的问题清单。每个回答检查是否包含：问题、约束、流程、状态、失败、取舍、边界。

当天输出：录音、卡顿点清单、需要回看代码的位置。

## Day 14：闭卷验收

闭卷完成：

- 画两张项目架构图；
- 画 Big-Market 抽奖到积分到账的全链路；
- 画 Haven 工具提议到安全执行的全链路；
- 回答 20 个追问；
- 为每个项目说出三个明确边界。

随后只回看答错的位置，不再从头重读。

## 之后的间隔复习

同一个主题按 `1 天 → 3 天 → 7 天 → 14 天` 复习。每次先闭卷画图和口述，再看代码。若回答中连续两次遗漏同一状态或失败窗口，将它单独做成卡片。

卡片正面不要写“什么是 Outbox”，而应写具体问题，例如：

> `saveUserAwardRecord` 已提交，但 MQ 发布前进程退出，中奖信息会不会丢？下一步由谁继续？

这种问题会迫使复习回到项目事实，而不是背通用定义。

