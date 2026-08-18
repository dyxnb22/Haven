"""后端按平台选择，并采用失败即关闭。"""

from haven.bootstrap import select_launcher


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
