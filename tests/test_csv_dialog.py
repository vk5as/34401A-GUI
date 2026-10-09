import re

from agilent34401a.gui.csv_dialog import default_csv_name


def test_a_suggested_csv_name_starts_with_the_stem_and_ends_with_the_local_time():
    assert re.fullmatch(r"34401A-\d{8}-\d{6}\.csv", default_csv_name())
    assert re.fullmatch(r"34401A-burst-\d{8}-\d{6}\.csv", default_csv_name("34401A-burst"))
