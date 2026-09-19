from pathlib import Path
import json
import os
import subprocess
import sys
import shutil

import pytest


ROOT = Path(__file__).parent.parent


def test_windows_launcher_probes_runtime_health_and_offers_recovery() -> None:
    launcher = (Path(__file__).parent.parent / "CMS.bat").read_text(encoding="utf-8")
    assert "assert sys.version_info >= (3, 11); import cms.cli" in launcher
    assert "if defined CMS_PYTHON" in launcher
    assert 'call :probe "%VENV_PY%"' in launcher
    assert 'call :probe "py" "-3"' in launcher
    assert 'call :probe "python"' in launcher
    assert "Setup-Atlas.bat" in launcher
    assert 'app --root "%~dp0."' in launcher
    assert "pip install" not in launcher
    assert launcher.index('call :probe "%ATLAS_VENV_PY%"') < launcher.index('call :probe "%VENV_PY%"')
    assert launcher.index(':no_runtime\n') < launcher.index(':finish\n')
    setup = (ROOT / "Setup-Atlas.bat").read_text(encoding="utf-8")
    assert 'pip install -e "%~dp0.[dev,anthropic]"' in setup
    assert '--no-pause' in setup


def test_windows_launcher_never_uses_venv_on_existence_alone() -> None:
    launcher = (Path(__file__).parent.parent / "CMS.bat").read_text(encoding="utf-8")
    assert 'set "CMS_PY=%~dp0.venv\\Scripts\\python.exe"' not in launcher
    assert launcher.index('call :probe "%VENV_PY%"') < launcher.index(
        '"%CMS_PY%" %CMS_PY_ARGS% -P -m cms.cli'
    )


def _launch(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CMS_PYTHON=sys.executable, USERPROFILE=str(tmp_path),
               PYTHONPATH="", NO_COLOR="1", TERM="dumb", PYTHONUTF8="1")
    # This competing local module must not shadow the checkout's application.
    (tmp_path / "cms.py").write_text("raise RuntimeError('wrong checkout')", encoding="utf-8")
    result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
                             str(ROOT / "CMS.bat"), *args], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry point")
def test_real_launcher_help_from_another_directory(tmp_path: Path) -> None:
    result = _launch(tmp_path, "--help")
    assert "context-evaluate" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry point")
def test_real_launcher_preserves_relative_explicit_root(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen project"
    chosen.mkdir()
    (chosen / "chosen_only.py").write_text("def hello(): return 1\n", encoding="utf-8")
    _launch(tmp_path, "scan", "--root", "chosen project")
    assert "chosen_only.py" in (chosen / ".memory" / "clean_tree.json").read_text(encoding="utf-8")
    assert not (tmp_path / ".memory").exists()
    result = _launch(tmp_path, "context", "--root", "chosen project")
    assert json.loads(result.stdout)["settings"]["mode"] == "off"


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry point")
@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_real_launcher_missing_runtime_shows_recovery(tmp_path: Path, arguments: list[str]) -> None:
    launcher = tmp_path / "CMS.bat"
    shutil.copyfile(ROOT / "CMS.bat", launcher)
    env = dict(os.environ, PATH=str(tmp_path), CMS_PYTHON=str(tmp_path / "missing.exe"),
               PYTHONPATH="")
    result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
                             str(launcher), *arguments], cwd=tmp_path, env=env,
                            input="\n", capture_output=True, text=True, timeout=15)
    assert result.returncode == 9009
    assert "Setup-Atlas.bat" in result.stdout
