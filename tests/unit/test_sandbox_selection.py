"""后端按平台选择，并采用失败即关闭。"""

import json
from dataclasses import replace
from pathlib import Path

from haven.adapters.sandbox.landlock import encode_spec
from haven.adapters.sandbox.seatbelt import build_profile
from haven.bootstrap import select_launcher
from haven.ports.sandbox import WRITABLE_DEVICES, SandboxSpec


class TestSelection:
    def test_macos_selects_seatbelt(self) -> None:
        launcher = select_launcher("darwin")
        assert launcher is not None
        assert launcher.backend == "seatbelt"

    def test_linux_selects_landlock(self) -> None:
        launcher = select_launcher("linux")
        assert launcher is not None
        assert launcher.backend == "landlock"

    def test_unsupported_platform_has_no_backend(self) -> None:
        """没有后端意味着拒绝 repo.exec，而不是不受限制地运行它。"""
        assert select_launcher("win32") is None


class TestBackendParity:
    """两个后端必须就"什么可写、什么可读"给出一致的答案。

    这里的断言全部建立在规格文本上，因此在任何平台都能发现不对称——而真实执行
    的断言只在有后端的平台上跑，开发机上总有一个后端是好的，不对称会被掩盖。
    """

    @staticmethod
    def _spec() -> SandboxSpec:
        return SandboxSpec(
            workspace_root=Path("/ws"),
            scratch_dir=Path("/scratch"),
            writable=True,
            allow_network=False,
        )

    def test_seatbelt_grants_the_writable_devices(self) -> None:
        """Seatbelt 显式放行这些设备：拒绝它们会破坏普通解释器（重定向到
        /dev/null 是最常见的一种）。"""
        seatbelt = build_profile(self._spec())
        for device in WRITABLE_DEVICES:
            resolved = str(Path(device).resolve())
            assert f'(allow file-write-data (literal "{resolved}"))' in seatbelt, (
                f"seatbelt 不再放行 {resolved}"
            )

    def test_landlock_cannot_express_a_per_device_grant(self) -> None:
        """Landlock 只有 `path_beneath` 一种规则，作用于文件层级。

        内核对 `/dev/null` 这类设备节点返回 EINVAL，因此这里**不能**照抄 Seatbelt——
        曾经照抄过，结果是所有沙箱命令以 125 退出。`cmd > /dev/null` 在 Linux 上
        不被允许，这是一条已记录在案的残留差异，不是疏漏。
        """
        landlock = json.loads(encode_spec(self._spec()))
        for device in WRITABLE_DEVICES:
            assert str(Path(device).resolve()) not in landlock["writable"]

    def test_landlock_covers_the_standard_system_roots(self) -> None:
        """/usr/local 是本地安装软件的默认位置；漏掉它，调用那里的工具会失败。"""
        landlock = json.loads(encode_spec(self._spec()))
        assert "/usr/local" in landlock["readable"]
        assert "/usr" in landlock["readable"]

    def test_the_workspace_is_writable_only_when_the_spec_says_so(self) -> None:
        for writable in (True, False):
            spec = replace(self._spec(), writable=writable)
            landlock = json.loads(encode_spec(spec))
            seatbelt = build_profile(spec)
            assert (str(spec.workspace_root) in landlock["writable"]) is writable
            assert (f'(allow file-write* (subpath "{spec.workspace_root}"))' in seatbelt) is (
                writable
            )
