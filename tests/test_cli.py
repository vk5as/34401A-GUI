import pytest

from agilent34401a import __version__
from agilent34401a.cli import main


def test_version_flag_prints_the_version_and_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help_and_succeeds(capsys):
    assert main([]) == 0

    assert "agilent34401a-cli" in capsys.readouterr().out
