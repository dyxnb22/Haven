"""Backend selection is per-platform and fails closed."""

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
        """No backend means repo.exec is denied, not that it runs unconfined."""
        assert select_launcher("win32") is None
