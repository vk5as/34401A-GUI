import pytest

from agilent34401a.gui.shortcuts import tk_sequences


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("F1", ["<Key-F1>"]),
        ("F11", ["<Key-F11>"]),
        ("Space", ["<Key-space>"]),
        ("R", ["<Key-r>", "<Key-R>"]),
        ("Ctrl+L", ["<Control-Key-l>", "<Control-Key-L>"]),
        ("Ctrl+,", ["<Control-Key-comma>"]),
        ("Ctrl+Shift+S", ["<Control-Shift-Key-S>"]),
        ("Alt+F4", ["<Alt-Key-F4>"]),
    ],
)
def test_a_shortcut_is_written_the_way_the_menus_show_it_and_bound_the_way_tk_wants_it(sequence, expected):
    assert tk_sequences(sequence) == expected


@pytest.mark.parametrize("sequence", ["", "Ctrl+", "Hyper+X", "Ctrl+Nonsense", "Ctrl+Ctrl+L"])
def test_a_shortcut_that_cannot_be_bound_is_refused(sequence):
    with pytest.raises(ValueError, match="shortcut"):
        tk_sequences(sequence)
