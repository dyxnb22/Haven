"""验证命令发现。

没有 .haven.toml 的全新仓库会使每次编辑都以 verification_unavailable 结束。发现
功能读取项目自身文件和浅层目录列表并提出检查配方，使人类可以注册它们。它绝不
运行任何命令，也不允许模型提供命令——检测由程序驱动，授权仍由用户掌握。

pytest 规则编码了针对五个真实仓库测得的经验（`docs/EVAL_LIVE.md`）：使用
`python -m pytest` 而不是裸二进制，以 tox.ini/setup.cfg 作为配置信号，使用
tests 目录作为结构回退，并为 src 布局修复 `pythonpath=src`。
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
        """在 idna 上测得：裸 `pytest` 不会将检出目录加入 sys.path，并会悄悄测试同一
        库的*已安装*副本。"""
        files = {"pyproject.toml": "[tool.pytest.ini_options]\n"}
        argv = pytest_argv(discover_recipes(files))
        assert argv is not None
        assert argv[0] != "pytest"
        assert argv[:3] == ("python", "-m", "pytest")


class TestStructuralFallback:
    def test_a_tests_directory_is_itself_a_signal(self) -> None:
        """在 jmespath 上测得：只有 setup.py 的打包没有任何配置信号，但有一个完全
        普通的 tests/ 目录。"""
        recipes = discover_recipes({}, ["tests/test_parser.py", "tests/__init__.py"])
        assert pytest_argv(recipes) == ("python", "-m", "pytest", "-q", "tests")

    def test_a_singular_test_directory_works_too(self) -> None:
        recipes = discover_recipes({}, ["test/test_output.py"])
        assert pytest_argv(recipes) == ("python", "-m", "pytest", "-q", "test")

    def test_src_layout_gets_the_import_path_repaired(self) -> None:
        """在 tomli 上测得：src/<pkg> 无法从裸检出目录导入；pytest 自身的 pythonpath
        覆盖可以修复它，而不需要生成 shim。"""
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
        """配置了 pytest 的项目对运行方式拥有权威；建议不得限定范围或覆盖其配置已
        经声明的内容。"""
        files = {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--doctest-modules'\n"}
        recipes = discover_recipes(files, ["tests/test_x.py", "src/pkg/__init__.py"])
        assert pytest_argv(recipes) == ("python", "-m", "pytest", "-q")

    def test_exactly_one_pytest_candidate(self) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\n", "tox.ini": "[pytest]\n"}
        recipes = discover_recipes(files, ["tests/test_x.py"])
        assert sum(1 for r in recipes if r.id == "pytest") == 1


class TestPluginWarnings:
    """环境无法运行的建议必须提前说明这一点。

    在 wcwidth 上测得：其 tox.ini addopts 要求 pytest-cov，因此除非安装该插件，
    建议命令会因用法错误退出——用户过去只有实际运行后才会发现。"""

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
