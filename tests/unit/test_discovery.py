"""Verification-command discovery.

A fresh repository with no .haven.toml makes every edit dead-end at
verification_unavailable. Discovery reads the project's own files plus a
shallow listing and proposes check recipes, so a human can register them. It
never runs anything and never lets the model supply a command — the detection
is program-driven and the authorization stays with the user.

The pytest rules encode lessons measured against five real repositories
(docs/EVAL_LIVE.md): `python -m pytest` rather than the bare binary, tox.ini /
setup.cfg as config signals, a tests-directory structural fallback, and
`pythonpath=src` repair for src layouts.
"""

from haven.domain.discovery import discover_recipes


def pytest_argv(recipes: list) -> tuple[str, ...] | None:  # type: ignore[type-arg]
    for recipe in recipes:
        if recipe.id == "pytest":
            return recipe.argv
    return None


class TestPytestConfigSignals:
    def test_pyproject_with_pytest_config_suggests_pytest(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"}
        assert pytest_argv(discover_recipes(files)) == ("python", "-m", "pytest", "-q")

    def test_pyproject_depending_on_pytest_suggests_pytest(self) -> None:
        files = {"pyproject.toml": 'dependencies = ["pytest>=8"]\n'}
        assert pytest_argv(discover_recipes(files)) is not None

    def test_plain_pyproject_without_pytest_does_not_guess(self) -> None:
        files = {"pyproject.toml": '[project]\nname = "x"\n'}
        assert pytest_argv(discover_recipes(files)) is None

    def test_tox_ini_pytest_section_is_a_signal(self) -> None:
        files = {"tox.ini": "[tox]\nenvlist = py312\n\n[pytest]\naddopts = -q\n"}
        assert pytest_argv(discover_recipes(files)) == ("python", "-m", "pytest", "-q")

    def test_setup_cfg_pytest_section_is_a_signal(self) -> None:
        files = {"setup.cfg": "[metadata]\nname = x\n\n[tool:pytest]\n"}
        assert pytest_argv(discover_recipes(files)) is not None

    def test_never_the_bare_pytest_binary(self) -> None:
        """Measured on idna: bare `pytest` leaves the checkout off sys.path and
        quietly tests the *installed* copy of the same library."""
        files = {"pyproject.toml": "[tool.pytest.ini_options]\n"}
        argv = pytest_argv(discover_recipes(files))
        assert argv is not None
        assert argv[0] != "pytest"
        assert argv[:3] == ("python", "-m", "pytest")


class TestStructuralFallback:
    def test_a_tests_directory_is_itself_a_signal(self) -> None:
        """Measured on jmespath: setup.py-only packaging, no config signal at
        all, but a perfectly ordinary tests/ directory."""
        recipes = discover_recipes({}, ["tests/test_parser.py", "tests/__init__.py"])
        assert pytest_argv(recipes) == ("python", "-m", "pytest", "-q", "tests")

    def test_a_singular_test_directory_works_too(self) -> None:
        recipes = discover_recipes({}, ["test/test_output.py"])
        assert pytest_argv(recipes) == ("python", "-m", "pytest", "-q", "test")

    def test_src_layout_gets_the_import_path_repaired(self) -> None:
        """Measured on tomli: src/<pkg> is not importable from a bare checkout;
        pytest's own pythonpath override fixes it without generated shims."""
        recipes = discover_recipes(
            {}, ["tests/test_data.py", "src/tomli/__init__.py", "src/tomli/_parser.py"]
        )
        assert pytest_argv(recipes) == (
            "python",
            "-m",
            "pytest",
            "-q",
            "-o",
            "pythonpath=src",
            "tests",
        )

    def test_a_directory_without_test_files_is_not_a_signal(self) -> None:
        assert pytest_argv(discover_recipes({}, ["tests/fixtures.json", "src/x.py"])) is None

    def test_config_signals_take_precedence_over_structure(self) -> None:
        """A project that configures pytest is the authority on how to run it;
        the suggestion must not scope or override what its config already says."""
        files = {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--doctest-modules'\n"}
        recipes = discover_recipes(files, ["tests/test_x.py", "src/pkg/__init__.py"])
        assert pytest_argv(recipes) == ("python", "-m", "pytest", "-q")

    def test_exactly_one_pytest_candidate(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\n", "tox.ini": "[pytest]\n"}
        recipes = discover_recipes(files, ["tests/test_x.py"])
        assert sum(1 for r in recipes if r.id == "pytest") == 1


class TestPluginWarnings:
    """A suggestion the environment cannot run needs to say so up front.

    Measured on wcwidth: its tox.ini addopts demand pytest-cov, so the
    suggested command dies with a usage error unless the plugin is installed —
    the user found out only by running it."""

    def test_cov_addopts_warn_about_pytest_cov(self) -> None:
        files = {"tox.ini": "[pytest]\naddopts = --cov=pkg --cov-report=html\n"}
        recipes = discover_recipes(files)
        assert recipes and "pytest-cov" in recipes[0].rationale

    def test_xdist_addopts_warn_about_pytest_xdist(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-n auto'\n"}
        recipes = discover_recipes(files)
        assert recipes and "pytest-xdist" in recipes[0].rationale

    def test_plain_config_carries_no_warning(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"}
        recipes = discover_recipes(files)
        assert recipes and "require" not in recipes[0].rationale


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
    def test_no_signals_yield_nothing(self) -> None:
        assert discover_recipes({}) == []
        assert discover_recipes({}, ["README.md", "src/x.py"]) == []

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
        listing = ["tests/test_a.py"]
        assert discover_recipes(files, listing) == discover_recipes(files, list(listing))
