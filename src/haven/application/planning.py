"""稳定步骤标识、依赖关系和计划验收证据的原子校验。"""

from haven.application.state import RunContext
from haven.contracts.tools import PlanStep


def normalize_plan(ctx: RunContext, steps: tuple[PlanStep, ...]) -> tuple[PlanStep, ...]:
    previous = {step.title: step.id for step in ctx.plan if step.id}
    reserved = {step.id for step in (*ctx.plan, *steps) if step.id}
    normalized: list[PlanStep] = []
    counter = 1
    for step in steps:
        step_id = step.id or previous.get(step.title, "")
        if not step_id:
            while f"step-{counter}" in reserved:
                counter += 1
            step_id = f"step-{counter}"
        reserved.add(step_id)
        normalized.append(
            step.model_copy(
                update={
                    "id": step_id,
                    "blocked_reason": step.blocked_reason if step.status == "blocked" else "",
                }
            )
        )
    by_id = {step.id: step for step in normalized}
    if len(by_id) != len(normalized):
        raise ValueError("plan step ids must be unique")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError("plan dependencies must not contain cycles")
        if step_id in visited:
            return
        visiting.add(step_id)
        step = by_id[step_id]
        for dependency in step.depends_on:
            if dependency not in by_id:
                raise ValueError(f"unknown dependency {dependency!r}")
            visit(dependency)
            if step.status in {"in_progress", "done"} and by_id[dependency].status != "done":
                raise ValueError(f"finish dependency {dependency!r} before starting {step_id!r}")
        visiting.remove(step_id)
        visited.add(step_id)

    last_edit = max((edit.seq for edit in ctx.ledger.edits), default=-1)
    for step in normalized:
        visit(step.id)
        if step.status == "blocked" and not step.blocked_reason.strip():
            raise ValueError("blocked steps must explain what is blocking them")
        if step.status == "done" and step.acceptance and not step.evidence:
            raise ValueError("completed steps with acceptance criteria require evidence")
        for reference in step.evidence:
            if reference.startswith("read:"):
                path = reference[5:]
                recorded = ctx.files_read.get(path)
                # `files_read` 记的是"读到时"的摘要；若此后该文件被改过，ledger 里
                # 最后一次编辑的 postimage 才是现在的摘要。两者不同，说明这次读取早于
                # 那次改动——它证明的是改动之前的状态，而步骤声称的恰恰是改动之后验收
                # 通过。`diff` 与 `check:` 一向要求证据晚于最后一次编辑，`read:` 没有
                # 理由例外：进程写入同样进 ledger，因此这条边界也覆盖外部改动。
                latest_edit = max(
                    (edit for edit in ctx.ledger.edits if edit.path == path),
                    key=lambda edit: edit.seq,
                    default=None,
                )
                valid = recorded is not None and (
                    latest_edit is None or recorded == latest_edit.postimage_digest
                )
            elif reference == "diff":
                valid = any(diff.seq > last_edit for diff in ctx.ledger.diffs)
            elif reference.startswith("check:"):
                latest = max(
                    (check for check in ctx.ledger.checks if check.recipe_id == reference[6:]),
                    key=lambda check: check.seq,
                    default=None,
                )
                valid = latest is not None and latest.exit_code == 0 and latest.seq > last_edit
            else:
                valid = False
            if not valid:
                raise ValueError(f"no fresh recorded evidence for {reference!r}")
    size = sum(
        len(step.title)
        + len(step.id)
        + len(step.blocked_reason)
        + sum(map(len, (*step.depends_on, *step.acceptance, *step.evidence)))
        for step in normalized
    )
    if size > 3200:
        raise ValueError("plan titles and details must total at most 3200 characters")
    return tuple(normalized)
