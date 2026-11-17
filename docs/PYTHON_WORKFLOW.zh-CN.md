# Python 工程：从发现配方到审阅修改

**中文** | [English](PYTHON_WORKFLOW.md)


当前走通的模板是 Python 3.12+、`src/` 布局、pytest 和 Ruff 的普通本地工程。
项目依赖和这两个检查工具需要事先安装在配方使用的 Python 环境中。
初始化和诊断不会安装依赖，也不会执行项目脚本。

## 1. 预览并确认配方

在项目根目录运行：

```bash
haven init
```

pytest 的配置、依赖声明或测试目录可触发已有 pytest 发现器。
Ruff 需要 `pyproject.toml` 中的 `[tool.ruff]`（包括其子表），
或根目录的 `ruff.toml` / `.ruff.toml`。
只声明任意名为 `lint` 的项目脚本不会触发 Ruff 发现。

例如以下项目配置：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
line-length = 100
```

会得到两个固定 argv 候选：

```toml
[recipes.pytest]
argv = ["python", "-m", "pytest", "-q"]

[recipes.ruff]
argv = ["python", "-m", "ruff", "check", "--no-cache", "."]
```

确认命令适合本项目后，用 `haven init --accept` 保存到 `.haven.toml`；
也可用 `haven discover` 预览、`haven discover --accept` 追加候选。
已有同 ID 配方保持原定义。发现只读取已知配置文件和浅层目录，
不会运行检查、项目脚本或解释器。

然后在 `.haven.toml` 加入本次可选的验证组：

```toml
[verification_groups]
quality = ["pytest", "ruff"]
```

## 2. 检查环境和执行范围

```bash
haven config explain
haven doctor
```

`config explain` 展示最终命令及其用户/项目配置文件来源、超时、
网络权限、配方声明的额外可读路径，以及统一的执行规则：

- 工作目录是运行的工作区根目录；启用隔离后是工作树根目录。
- 环境只转发 `PATH`、`HOME`、`LANG`、`LC_ALL`、`TERM`、`TMPDIR`、
  `VIRTUAL_ENV`；解释时只显示变量名。
- 子进程固定使用 `CI=1`、`DEBIAN_FRONTEND=noninteractive`、
  `TERM=dumb`、`NO_COLOR=1`，`TMPDIR` 指向工作区之外的独立临时目录。
- 解释器默认可读根目录单独列出；配方额外路径在执行时展开 `~` 并解析。
  网络默认拒绝，仍由原有审批与操作系统沙箱执行限制。

`doctor` 检查可执行文件；对 `python -m pytest` / `python -m ruff`，
若解析出的解释器属于 Haven 当前环境，还会在该环境安装目录中查找模块。
缺少模块时退出码为 2，并提示先准备项目依赖再检查。
查找不会导入仓库里的同名模块，也不运行模块正文。

如果配方使用另一个 Python 环境，诊断明确显示依赖可用性未知。
这时需要在对应环境中自行确认工具和项目依赖，例如运行
`python -m pytest --version`、`python -m ruff --version`。
模块存在也不保证所有项目依赖完整；实际检查结果仍是判断依据。

## 3. 修改、完成两项验证、审阅

TUI 中先用 `/verify use quality` 选择下一次运行的验收范围，再输入任务；
用 `/verify` 查看两项结果。选择不会自动执行命令或增加权限。

需要保留主工作区供审阅时，可使用现有无头流程：

```bash
haven run "修复 add 的结果并移除无用 import" \
  --write --approval-policy all --check-group quality \
  --worktree --review --json --events /tmp/haven-python-events.jsonl
```

`--approval-policy all` 是本次明确授权所有经策略允许的审批请求；
交互式逐项审批可在 TUI 中完成。检查依然使用注册配方和统一执行通道。
事件中的 `run.created.workspace` 记录实际工作目录。
每个检查都必须在最后一次写入之后按完整 argv 通过；
漏项、失败、过期或追加筛选参数均不能满足所选组。

成功后，JSON 中 `verification` 列出两项状态；`review_required` 表示仍需审阅。
将实际运行 ID 和查看后得到的 binding 代入：

```bash
haven review RUN_ID
haven review RUN_ID --apply --binding REVIEW_BINDING
```

查看不会应用修改，应用绑定到已查看的具体快照。
从项目目录之外执行上述命令时，均添加 `--workspace /path/to/project`。
组的恢复、后续轮次继承和清除规则见[验证配置组](VERIFICATION_GROUPS.md)。

## 离线验收

[一个临时 Python 项目](../tests/integration/test_python_workflow.py)
覆盖了初始化预览、确认和重复发现、只读诊断、真实 pytest/Ruff 失败与通过、
工作树隔离及显式审阅应用。只使用脚本模型，实际执行两个检查工具；
测试记录传给沙箱的限制，操作系统层面的限制由独立安全测试验证。
额外的进程环境变量不会进入检查子进程。

```bash
uv run pytest -q tests/integration/test_python_workflow.py
```

这一夹具用于验证工具流程，不测量真实模型的任务成功率。
