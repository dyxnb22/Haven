"""Verification-command discovery.

A fresh repository with no .haven.toml makes every edit dead-end at
verification_unavailable. Discovery reads the project's own files and proposes
check recipes, so a human can register them. It never runs anything and never
lets the model supply a command — the detection is program-driven and the
authorization stays with the user.
"""

from haven.domain.discovery import discover_recipes


class TestPython:
    def test_pyproject_with_pytest_suggests_pytest(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"}
        recipes = discover_recipes(files)
        assert any(r.argv[:1] == ("pytest",) for r in recipes)

    def test_pyproject_depending_on_pytest_suggests_pytest(self) -> None:
        files = {"pyproject.toml": 'dependencies = ["pytest>=8"]\n'}
        assert any(r.argv[0] == "pytest" for r in discover_recipes(files))

    def test_plain_pyproject_without_pytest_does_not_guess(self) -> None:
        files = {"pyproject.toml": '[project]\nname = "x"\n'}
        assert all(r.argv[0] != "pytest" for r in discover_recipes(files))


class TestOtherEcosystems:
    def test_package_json_test_script(self) -> None:
        files = {"package.json": '{"scripts": {"test": "vitest run"}}'}
        recipes = discover_recipes(files)
        assert any(r.argv[:2] == ("npm", "test") for r in recipes)

    def test_package_json_without_test_script_is_ignored(self) -> None:
        files = {"package.json": '{"scripts": {"build": "tsc"}}'}
        assert all("npm" not in r.argv for r in discover_recipes(files))

    def test_cargo_suggests_cargo_test(self) -> None:
        assert any(r.argv == ("cargo", "test") for r in discover_recipes({"Cargo.toml": ""}))

    def test_go_mod_suggests_go_test(self) -> None:
        assert any(r.argv[:2] == ("go", "test") for r in discover_recipes({"go.mod": "module x"}))

    def test_makefile_with_test_target(self) -> None:
        files = {"Makefile": "build:\n\tcc x\n\ntest:\n\t./run\n"}
        assert any(r.argv == ("make", "test") for r in discover_recipes(files))

    def test_makefile_without_test_target_is_ignored(self) -> None:
        assert not any("make" in r.argv for r in discover_recipes({"Makefile": "build:\n\tcc x\n"}))


class TestShape:
    def test_no_project_files_yields_nothing(self) -> None:
        assert discover_recipes({}) == []

    def test_candidates_have_stable_unique_ids(self) -> None:
        files = {
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "Cargo.toml": "",
        }
        recipes = discover_recipes(files)
        ids = [r.id for r in recipes]
        assert len(ids) == len(set(ids))
        assert all(r.id and r.argv for r in recipes)

    def test_results_are_deterministic(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\n", "go.mod": "module x"}
        assert discover_recipes(files) == discover_recipes(dict(files))
