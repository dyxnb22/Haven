# Agent 校招应用项目选题调研

日期：2026-09-26。本文是选题建议，不是 Haven 架构变更或实施计划。

## 范围与证据边界

用户已有自研编码 Agent Haven，并规划了 knowledge 与 browser 服务；希望补充一个 LangGraph 应用，避免重复通用 Agent，不做评估工作台。

仅访问公开资料。未访问知识星球会员内容，无全平台热度排名。面经和履历属于作者自述，未核实身份、录用结果和业务指标。课程宣传不证明求职效果。公开样本不足以统计整个校招市场。

## 公开项目观察

- [鱼皮 yu-ai-agent](https://github.com/liyupi/yu-ai-agent)：作者公开仓库介绍了 Spring AI、RAG、工具调用、MCP 和 ReAct 智能体。它说明已有覆盖广泛技术点的教程项目；不能推导完成教程即可获得录用。
- [小傅哥 AI Agent 脚手架与场景应用](https://origin.bugstack.cn/md/zsxq/project/ai-agent-scaffold.html)：公开课程介绍包含 Spring AI、LangChain4j、Google ADK、MCP、Skills 和编排；[星球介绍](https://bugstack.cn/md/zsxq/introduce.html)列出网关、RAG/Agent、代码评审等项目。本文未审阅付费源码。
- [公开 Agent 产品作品集](https://daiyuxin26.github.io/)：描述了 LangGraph 业务管理应用、角色权限、任务流转和人工确认。不能认定它属于校招生，其效率数字未独立验证，仅作为作品表达的参考。
- [字节数据中台 Agent 全栈面经](https://www.nowcoder.com/feed/main/detail/fe77496948f74b4091ab4bb6e8156c8b)：搜索索引可读摘录提及实习项目深挖、Skill/Tool 边界、LangChain/LangGraph 区别。原页面打开失败，只作有限的自述样本。
- [字节 Agent 开发后端实习面经](https://www.nowcoder.com/discuss/932295617999622144?sourceSSR=home)：带参数链接成功打开全文。作者自述两个 Agent/RAG 项目被追问选型、效果依据，以及除技术栈之外的业务差异。身份与经历未独立验证。
- [曹操出行 Agent 实习面经](https://www.nowcoder.com/feed/main/detail/1650bd48a48644138ebf2362740abc86)：并行研究读取到正文，作者自述被问到脚本可以完成的提单流程为何需要子图 Agent、编排与业务的关系、长链路稳定性。主会话复查只返回页面导航，存在动态页面访问差异。

## 推荐课题：基于 LangGraph 的 CI 失败诊断与修复交接系统

暂名 haven-triage，独立项目。输入失败运行、对应提交与日志，收集证据并形成可审核的诊断；获得授权后可委托 Haven 修复。第一版即使未接 Haven，也能交付诊断。

这是结合用户现有项目作出的设计判断，不是调查得出的行业热门排名。

推荐理由：CI 日志、提交和检查结果容易在自己的示例仓库获得；业务范围比完整生产运维小；可通过已知故障验证；与 Haven 的编码执行分工明确。通用 Agent 也能分析日志，本项目的增量应落在事件关联、状态持久化、补充取证、审批、去重与版本变化处理。

流程：失败输入 → 解析与脱敏 → 按需并行收集日志/变更/历史手册 → 假设与证据 → 有界补充取证 → 诊断或未知 → 人工确认 → 可选修复交接 → 验证与归档。

固定解析和策略使用确定性代码；模型负责综合不完整证据、提出假设和选择下一项允许的检查。图节点不等于独立 Agent，不为多 Agent 关键词制造角色。

## 框架依据与约束

[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)提供持久化相关能力；[interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)支持暂停并接收外部输入。官方接口说明恢复会从节点开头重新执行，因此副作用必须幂等，不能宣称检查点带来外部操作 exactly-once。

建议状态包含运行与提交标识、证据引用、假设、已执行检查、预算、待审批方案和最终结论。日志与代码视为不可信输入；模型不能自行授权命令。方案审批绑定提交和方案内容，提交变化后重新检查。

## 最小范围

1. 单个 Python 示例仓库、pytest 与 GitHub Actions 风格日志；先支持离线导入。
2. 覆盖代码断言失败、依赖问题、配置缺失；允许明确输出未知，不能仅凭一段日志认定根因。
3. LangGraph + FastAPI + PostgreSQL；轻量界面显示证据、状态与确认操作。
4. 固定只读取证工具；后续通过显式接口接 knowledge 与 Haven。
5. 不建 Kubernetes 运维平台，不做通用 DAG 编辑器，不自动部署或合并。
6. browser 仅在需要验证本地网页场景时接入；不把所有已有服务强行加入首版。

## 建议验收

使用自己构造并标明来源的故障案例，记录诊断是否匹配、是否正确输出未知、耗时、调用成本。额外验证重复事件、重启恢复、过期审批和中断的外部动作。数据与模型效果如实报告，不承诺预设准确率，不搭建独立评估工作台。

项目展示目标：输入失败运行，展示证据与诊断，中断重启后恢复，人工确认后交给 Haven，最后返回指定提交上的实际检查结果。这个目标目前尚未实现。
