"""SBPL profile 是 spec 的纯函数，因此无需运行任何内容即可断言。规则顺序很重要：
在 SBPL 中最后匹配的规则生效。"""

from pathlib import Path

from haven.adapters.sandbox.seatbelt import SeatbeltLauncher, build_profile
from haven.ports.sandbox import SandboxSpec


def spec(**overrides: object) -> SandboxSpec:
    base: dict[str, object] = {
        "workspace_root": Path("/tmp/ws"),
        "scratch_dir": Path("/tmp/scratch"),
        "writable": True,
    }
    base.update(overrides)
    return SandboxSpec(**base)  # type: ignore[arg-type]


def index_of(profile: str, needle: str) -> int:
    position = profile.find(needle)
    assert position >= 0, f"{needle!r} missing from profile:\n{profile}"
    return position


def resolved(path: str) -> str:
    return str(Path(path).resolve())


class TestProfile:
    def test_denies_by_default(self) -> None:
        assert "(deny default)" in build_profile(spec())

    def test_workspace_is_writable(self) -> None:
        assert f'(allow file-write* (subpath "{resolved("/tmp/ws")}")' in build_profile(spec())

    def test_read_only_spec_grants_scratch_but_not_the_workspace(self) -> None:
        """只读指工作区只读：scratch 仍可写，使受限进程有地方写入（repo.exec profile，
        ADR 0017）。"""
        profile = build_profile(spec(writable=False))
        assert f'(allow file-write* (subpath "{resolved("/tmp/scratch")}")' in profile
        assert f'(allow file-write* (subpath "{resolved("/tmp/ws")}")' not in profile

    def test_scratch_is_writable(self) -> None:
        assert f'(allow file-write* (subpath "{resolved("/tmp/scratch")}")' in build_profile(spec())

    def test_network_denied_by_default(self) -> None:
        assert "(deny network*)" in build_profile(spec())

    def test_network_allowed_when_requested(self) -> None:
        assert "(deny network*)" not in build_profile(spec(allow_network=True))

    def test_protected_paths_are_denied_after_the_workspace_grant(self) -> None:
        """后面的规则优先，因此例外范围必须放在授权规则之后。"""
        profile = build_profile(spec())
        assert index_of(
            profile, f'(deny file-write* (subpath "{resolved("/tmp/ws")}/.git")'
        ) > index_of(profile, f'(allow file-write* (subpath "{resolved("/tmp/ws")}")')

    def test_private_roots_are_denied_after_the_broad_read_grant(self) -> None:
        profile = build_profile(spec(private_roots=(Path("/tmp/home"),)))
        assert index_of(
            profile, f'(deny file-read* (subpath "{resolved("/tmp/home")}")'
        ) > index_of(profile, "(allow file-read*)")

    def test_workspace_read_is_restored_after_a_private_root_denial(self) -> None:
        """位于被拒绝主目录中的工作区仍必须可读。"""
        profile = build_profile(
            spec(workspace_root=Path("/tmp/home/ws"), private_roots=(Path("/tmp/home"),))
        )
        assert index_of(
            profile, f'(allow file-read* (subpath "{resolved("/tmp/home/ws")}")'
        ) > index_of(profile, f'(deny file-read* (subpath "{resolved("/tmp/home")}")')

    def test_extra_readable_roots_are_granted(self) -> None:
        profile = build_profile(spec(extra_readable_roots=(Path("/tmp/py"),)))
        assert f'(allow file-read* (subpath "{resolved("/tmp/py")}")' in profile

    def test_quotes_in_a_path_cannot_inject_a_rule(self) -> None:
        profile = build_profile(spec(scratch_dir=Path('/tmp/a"(allow default)')))
        assert '\\"' in profile
        assert "\n(allow default)" not in profile


class TestLauncher:
    def test_wrap_invokes_sandbox_exec(self) -> None:
        wrapped = SeatbeltLauncher().wrap(("ls", "-la"), spec())
        assert wrapped[0] == "/usr/bin/sandbox-exec"
        assert wrapped[1] == "-p"
        assert wrapped[3:] == ("ls", "-la")

    def test_backend_name(self) -> None:
        assert SeatbeltLauncher().backend == "seatbelt"

    def test_describe_states_the_confinement(self) -> None:
        description = SeatbeltLauncher().describe(spec())
        assert "seatbelt" in description
        assert "no network" in description
