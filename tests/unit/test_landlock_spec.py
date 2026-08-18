"""每个平台都会断言载荷和包装；真实内核限制由
tests/security/test_sandbox_enforcement.py 证明。"""

import json
import sys
from pathlib import Path

from haven.adapters.sandbox.landlock import LandlockLauncher, encode_spec
from haven.ports.sandbox import SandboxSpec


def spec(**overrides: object) -> SandboxSpec:
    base: dict[str, object] = {
        "workspace_root": Path("/tmp/ws"),
        "scratch_dir": Path("/tmp/scratch"),
        "writable": True,
    }
    base.update(overrides)
    return SandboxSpec(**base)  # type: ignore[arg-type]


def resolved(path: str) -> str:
    return str(Path(path).resolve())


class TestEncoding:
    def test_writable_roots_are_the_workspace_and_scratch(self) -> None:
        payload = json.loads(encode_spec(spec()))
        assert sorted(payload["writable"]) == sorted(
            [resolved("/tmp/scratch"), resolved("/tmp/ws")]
        )

    def test_read_only_spec_grants_scratch_but_not_the_workspace(self) -> None:
        """只读指工作区只读：scratch 仍可写，使受限进程有地方写入（repo.exec profile，
        ADR 0017）。"""
        writable = json.loads(encode_spec(spec(writable=False)))["writable"]
        assert writable == [str(Path("/tmp/scratch").resolve())]

    def test_system_roots_are_readable(self) -> None:
        assert "/usr" in json.loads(encode_spec(spec()))["readable"]

    def test_private_roots_are_never_granted(self) -> None:
        """Landlock 权限是累加的：没有被纳入的内容就是受限内容。"""
        payload = json.loads(encode_spec(spec(private_roots=(Path("/tmp/home"),))))
        assert resolved("/tmp/home") not in payload["readable"]

    def test_workspace_is_readable_even_inside_a_private_root(self) -> None:
        payload = json.loads(
            encode_spec(
                spec(workspace_root=Path("/tmp/home/ws"), private_roots=(Path("/tmp/home"),))
            )
        )
        assert resolved("/tmp/home/ws") in payload["readable"]

    def test_extra_readable_roots_are_granted(self) -> None:
        payload = json.loads(encode_spec(spec(extra_readable_roots=(Path("/tmp/py"),))))
        assert resolved("/tmp/py") in payload["readable"]

    def test_network_flag_is_carried(self) -> None:
        assert json.loads(encode_spec(spec()))["allow_network"] is False
        assert json.loads(encode_spec(spec(allow_network=True)))["allow_network"] is True


class TestLauncher:
    def test_wrap_reexecs_through_the_launcher_module(self) -> None:
        wrapped = LandlockLauncher().wrap(("ls", "-la"), spec())
        assert wrapped[0] == sys.executable
        assert wrapped[1:3] == ("-m", "haven.sandbox.landlock_launcher")
        assert wrapped[-3:] == ("--", "ls", "-la")

    def test_backend_name(self) -> None:
        assert LandlockLauncher().backend == "landlock"

    def test_describe_names_the_platform_limitation(self) -> None:
        """Landlock 无法表达 .git 例外范围；描述中必须说明这一点。"""
        description = LandlockLauncher().describe(spec())
        assert "landlock" in description
        assert ".git" in description

    def test_unavailable_off_linux(self) -> None:
        if not sys.platform.startswith("linux"):
            assert LandlockLauncher().available() is False
