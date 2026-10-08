"""The README makes promises about files, links, commands, tabs and keys; these tests keep them true.

The seams are the README text on one side and, on the other, what a user can observe: the files in the repository,
each tool's `--help`, the window's tabs and its Help → Shortcuts table, and the names of the settings files.
"""

import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

from agilent34401a.cli import main as cli_main
from agilent34401a.gui.app import main as gui_main
from agilent34401a.preset_store import PRESETS_FILE
from agilent34401a.settings import SETTINGS_FILE
from agilent34401a.sim_server import main as sim_main

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
IMAGES = ROOT / "docs" / "images"
TEXT = README.read_text(encoding="utf-8")

_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
_FENCE = re.compile(r"^```[a-z]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_INLINE_OPTION = re.compile(r"`(--[a-z][a-z-]*)")
_TOOLS = {"agilent34401a-cli": cli_main, "agilent34401a-sim": sim_main, "agilent34401a-gui": gui_main}


def _slug(heading: str) -> str:
    """How GitHub names the anchor of a heading."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _links() -> list[tuple[bool, str, str]]:
    return [(bool(bang), text, target) for bang, text, target in _LINK.findall(TEXT)]


def _help(main, *arguments: str, monkeypatch) -> str:
    monkeypatch.setenv("COLUMNS", "1000")  # no wrapping, so an option is never split at a hyphen
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as stopped:
        main([*arguments, "--help"])
    assert stopped.value.code == 0
    return output.getvalue()


def _commands() -> list[list[str]]:
    """Every shell line in a code block that runs one of the project's own tools, as shell words."""
    found = []
    for block in _FENCE.findall(TEXT):
        for line in block.splitlines():
            words = shlex.split(line, comments=True)
            for index, word in enumerate(words):
                if Path(word).name in _TOOLS:
                    rest = words[index:]
                    for stop in (">", "&", "|"):
                        if stop in rest:
                            rest = rest[: rest.index(stop)]
                    found.append([Path(rest[0]).name, *rest[1:]])
                    break
    return found


def _subcommands(monkeypatch) -> list[str]:
    listing = re.search(r"\{([a-z,]+)\}", _help(cli_main, monkeypatch=monkeypatch))
    assert listing is not None
    return listing.group(1).split(",")


def test_every_image_and_link_to_a_file_in_the_readme_exists():
    for _is_image, _text, target in _links():
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        assert (ROOT / target.split("#")[0]).exists(), f"README links to {target}, which is not in the repository"


def test_every_screenshot_has_alt_text_and_lives_in_docs_images():
    images = [(text, target) for is_image, text, target in _links() if is_image]
    assert images
    for text, target in images:
        assert text.strip(), f"The image {target} has no alt text"
        assert target.startswith("docs/images/")


def test_every_picture_in_docs_images_is_used_by_the_readme():
    used = {Path(target).name for is_image, _text, target in _links() if is_image}
    assert {path.name for path in IMAGES.glob("*.png")} <= used


def test_every_link_within_the_readme_goes_to_a_heading():
    anchors = {_slug(heading) for heading in _HEADING.findall(TEXT)}
    for _is_image, _text, target in _links():
        if target.startswith("#"):
            assert target[1:] in anchors, f"README links to {target}, which is no heading"


def test_every_command_the_readme_shows_is_accepted_by_the_tool_it_names(monkeypatch):
    subcommands = _subcommands(monkeypatch)
    commands = _commands()
    assert commands
    for tool, *words in commands:
        flags = [word.split("=")[0] for word in words if word.startswith("--")]
        if tool == "agilent34401a-cli" and words and not words[0].startswith("-"):
            assert words[0] in subcommands, f"README runs {tool} {words[0]}, which is not a subcommand"
            shown = _help(cli_main, words[0], monkeypatch=monkeypatch)
        else:
            shown = _help(_TOOLS[tool], monkeypatch=monkeypatch)
        for flag in flags:
            assert flag in shown or flag in {
                "--help",
                "--version",
            }, f"README passes {flag} to {tool}: not in its --help"


def test_every_option_the_readme_names_in_prose_exists_in_some_tool(monkeypatch):
    shown = _help(sim_main, monkeypatch=monkeypatch) + _help(gui_main, monkeypatch=monkeypatch)
    for subcommand in _subcommands(monkeypatch):
        shown += _help(cli_main, subcommand, monkeypatch=monkeypatch)
    for option in set(_INLINE_OPTION.findall(TEXT)):
        assert option in shown or option == "--help", f"README names {option}, which no tool has"


def test_the_readme_shows_every_cli_subcommand(monkeypatch):
    shown = {
        tool_command[1]
        for tool_command in _commands()
        if tool_command[0] == "agilent34401a-cli" and len(tool_command) > 1
    }
    assert set(_subcommands(monkeypatch)) <= shown


def test_the_readme_names_the_files_settings_and_presets_are_kept_in():
    assert f"`{SETTINGS_FILE}`" in TEXT
    assert f"`{PRESETS_FILE}`" in TEXT


def test_the_readme_describes_every_tab_of_the_window(make_window):
    window = make_window()
    titles = [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()]
    assert titles
    for title in titles:
        assert f"**{title}**" in TEXT, f"README does not describe the {title} tab"


def test_the_readme_lists_the_shortcuts_the_window_has_and_no_others(make_window):
    window = make_window()
    available = {shortcut.sequence for shortcut in window.shortcuts.entries() if shortcut.available}
    planned = {shortcut.sequence for shortcut in window.shortcuts.entries() if not shortcut.available}
    rows = {
        cell.strip()
        for line in TEXT.split("## Keyboard shortcuts")[1].split("###")[0].splitlines()
        if line.startswith("| ")
        for cell in line.split("|")[1:2]
    }
    functions = {sequence for sequence in available if re.fullmatch(r"F\d+", sequence)}
    assert "F1 to F11" in rows
    assert len(functions) == 11
    assert rows - {"Shortcut", "---", "F1 to F11"} == available - functions
    for sequence in planned:
        assert (
            f"{sequence} (" in TEXT.split("## Keyboard shortcuts")[1]
        ), f"README does not say {sequence} is not there yet"
