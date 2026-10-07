import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# The script reads pyproject.toml with tomllib (Python 3.11+); the Release workflow runs it on 3.13.
pytest.importorskip("tomllib")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_info.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_info", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_info"] = module
    spec.loader.exec_module(module)
    return module


release_info = _load_script()


def write_pyproject(directory: Path, version: str = "1.2.3") -> Path:
    path = directory / "pyproject.toml"
    path.write_text(f'[project]\nname = "agilent34401a"\nversion = "{version}"\n', encoding="utf-8")
    return path


def write_distributions(directory: Path, version: str, *, wheel: bool = True, sdist: bool = True) -> None:
    if wheel:
        (directory / f"agilent34401a-{version}-py3-none-any.whl").write_bytes(b"")
    if sdist:
        (directory / f"agilent34401a-{version}.tar.gz").write_bytes(b"")


def test_the_release_version_is_read_from_pyproject(tmp_path):
    assert release_info.read_version(write_pyproject(tmp_path, "0.4.1")) == "0.4.1"


def test_the_release_tag_is_the_version_prefixed_with_v():
    assert release_info.release_tag("0.4.1") == "v0.4.1"


def test_the_real_pyproject_yields_a_release_tag():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"

    assert release_info.release_tag(release_info.read_version(pyproject)).startswith("v")


def test_a_pyproject_without_a_version_is_rejected(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "agilent34401a"\n', encoding="utf-8")

    with pytest.raises(release_info.ReleaseError, match="version"):
        release_info.read_version(path)


@pytest.mark.parametrize("version", ["", "v1.0.0", "1.0.0 ", "1.0/../x", "latest", "1"])
def test_a_version_that_is_unsafe_as_a_tag_is_rejected(tmp_path, version):
    with pytest.raises(release_info.ReleaseError, match="version"):
        release_info.read_version(write_pyproject(tmp_path, version))


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0rc1", "2.3.4.post1", "1.0.0.dev2", "1.0"])
def test_ordinary_pep_440_versions_are_accepted(tmp_path, version):
    assert release_info.read_version(write_pyproject(tmp_path, version)) == version


def test_the_distributions_are_one_wheel_and_one_sdist_of_the_release_version(tmp_path):
    write_distributions(tmp_path, "1.2.3")

    wheel, sdist = release_info.find_distributions(tmp_path, "1.2.3")

    assert wheel.name == "agilent34401a-1.2.3-py3-none-any.whl"
    assert sdist.name == "agilent34401a-1.2.3.tar.gz"


def test_a_missing_wheel_is_reported(tmp_path):
    write_distributions(tmp_path, "1.2.3", wheel=False)

    with pytest.raises(release_info.ReleaseError, match="wheel"):
        release_info.find_distributions(tmp_path, "1.2.3")


def test_a_missing_sdist_is_reported(tmp_path):
    write_distributions(tmp_path, "1.2.3", sdist=False)

    with pytest.raises(release_info.ReleaseError, match="sdist"):
        release_info.find_distributions(tmp_path, "1.2.3")


def test_distributions_of_another_version_are_rejected(tmp_path):
    write_distributions(tmp_path, "1.2.3")
    write_distributions(tmp_path, "1.2.2")

    with pytest.raises(release_info.ReleaseError, match=r"1\.2\.2"):
        release_info.find_distributions(tmp_path, "1.2.3")


def test_a_wheel_that_is_not_universal_is_rejected(tmp_path):
    (tmp_path / "agilent34401a-1.2.3-cp313-cp313-win_amd64.whl").write_bytes(b"")
    (tmp_path / "agilent34401a-1.2.3.tar.gz").write_bytes(b"")

    with pytest.raises(release_info.ReleaseError, match="wheel"):
        release_info.find_distributions(tmp_path, "1.2.3")


def test_the_tag_command_prints_only_the_tag(tmp_path, capsys):
    pyproject = write_pyproject(tmp_path, "0.9.0")

    assert release_info.main(["tag", "--pyproject", str(pyproject)]) == 0

    assert capsys.readouterr().out == "v0.9.0\n"


def test_the_version_command_prints_only_the_version(tmp_path, capsys):
    pyproject = write_pyproject(tmp_path, "0.9.0")

    assert release_info.main(["version", "--pyproject", str(pyproject)]) == 0

    assert capsys.readouterr().out == "0.9.0\n"


def test_the_check_dist_command_succeeds_when_the_distributions_match(tmp_path, capsys):
    pyproject = write_pyproject(tmp_path, "0.9.0")
    write_distributions(tmp_path, "0.9.0")

    assert release_info.main(["check-dist", str(tmp_path), "--pyproject", str(pyproject)]) == 0

    assert "0.9.0" in capsys.readouterr().out


def test_the_check_dist_command_fails_on_stderr_when_the_distributions_do_not_match(tmp_path, capsys):
    pyproject = write_pyproject(tmp_path, "0.9.0")
    write_distributions(tmp_path, "0.8.0")

    assert release_info.main(["check-dist", str(tmp_path), "--pyproject", str(pyproject)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "0.8.0" in captured.err


def test_a_missing_pyproject_fails_on_stderr(tmp_path, capsys):
    assert release_info.main(["tag", "--pyproject", str(tmp_path / "absent.toml")]) == 1

    assert "absent.toml" in capsys.readouterr().err
