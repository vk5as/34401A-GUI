"""The Preset store: Presets kept in the config folder, and exported to and imported from files."""

import json
from pathlib import Path

import pytest

from agilent34401a.errors import PresetError
from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import AcFilter, Function, Setup
from agilent34401a.preset import FORMAT_VERSION, MAX_NAME_LENGTH
from agilent34401a.preset_store import PRESETS_FILE, Collision, PresetStore
from agilent34401a.settings import config_dir
from agilent34401a.trigger import TriggerSettings, TriggerSource

DC_VOLTS = Setup.default(Function.DC_VOLTAGE).with_range(10.0).with_nplc(1)
AC_VOLTS = Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.FAST)
BURST = (
    Setup.default(Function.RESISTANCE_4W)
    .with_trigger(TriggerSettings(TriggerSource.BUS, delay=0.5, sample_count=4, trigger_count=None))
    .with_math(MathSettings(operation=MathOperation.NULL, null_offset=1.25))
)


@pytest.fixture
def store(tmp_path: Path) -> PresetStore:
    return PresetStore.load(tmp_path / "config")


def reloaded(store: PresetStore) -> PresetStore:
    assert store.path is not None
    return PresetStore.load(store.path.parent)


def stray_files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name != PRESETS_FILE)


# --- saving and loading --------------------------------------------------------------------------------------------


def test_a_new_store_has_no_presets_and_no_problems(store):
    assert store.presets == ()
    assert store.names == ()
    assert store.problems == ()


def test_a_saved_preset_comes_back_when_the_store_is_loaded_again(store):
    store.save("Bench supply", DC_VOLTS)
    store.save("Mains", AC_VOLTS)
    store.save("Burst of ohms", BURST)

    again = reloaded(store)

    assert again.names == ("Bench supply", "Mains", "Burst of ohms")
    assert again.get("Bench supply").setup == DC_VOLTS
    assert again.get("Mains").setup == AC_VOLTS
    assert again.get("Burst of ohms").setup == BURST


def test_the_presets_file_is_versioned_json_beside_the_settings(store):
    store.save("Bench supply", DC_VOLTS)

    assert store.path is not None
    written = json.loads(store.path.read_text(encoding="utf-8"))
    assert store.path.name == "presets.json"
    assert written["version"] == FORMAT_VERSION == 1
    assert written["format"] == "agilent34401a-presets"
    assert [entry["name"] for entry in written["presets"]] == ["Bench supply"]
    assert written["presets"][0]["setup"]["function"] == "VOLT:DC"


def test_the_default_folder_is_the_platform_config_folder():
    store = PresetStore.load()

    assert store.path == config_dir() / PRESETS_FILE


def test_a_store_in_memory_never_touches_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = PresetStore.in_memory()

    store.save("Bench supply", DC_VOLTS)

    assert store.path is None
    assert store.get("Bench supply").setup == DC_VOLTS
    assert list(tmp_path.iterdir()) == []


def test_a_preset_is_found_by_name_whatever_the_case(store):
    store.save("Bench Supply", DC_VOLTS)

    assert "bench supply" in store
    assert store.get("BENCH SUPPLY").name == "Bench Supply"


def test_a_preset_that_is_not_there_is_an_error_naming_it(store):
    with pytest.raises(PresetError, match="no Preset called 'Nope'"):
        store.get("Nope")


# --- names and duplicates ------------------------------------------------------------------------------------------


def test_a_name_is_trimmed(store):
    assert store.save("  Bench supply \n", DC_VOLTS).name == "Bench supply"


@pytest.mark.parametrize("name", ["", "   ", "a" * (MAX_NAME_LENGTH + 1), "two\nlines", "tab\there"])
def test_a_name_that_is_empty_too_long_or_has_control_characters_is_refused(store, name):
    with pytest.raises(PresetError, match="name"):
        store.save(name, DC_VOLTS)
    assert store.presets == ()


def test_a_name_in_use_is_refused_unless_the_preset_is_to_be_replaced(store):
    store.save("Bench supply", DC_VOLTS)

    with pytest.raises(PresetError, match="already a Preset called 'Bench supply'"):
        store.save("bench SUPPLY", AC_VOLTS)

    assert store.get("Bench supply").setup == DC_VOLTS


def test_replacing_a_preset_keeps_its_place_and_takes_the_new_setup_and_name(store):
    store.save("First", DC_VOLTS)
    store.save("Second", DC_VOLTS)
    store.save("Third", DC_VOLTS)

    store.save("second", AC_VOLTS, replace=True)

    assert reloaded(store).names == ("First", "second", "Third")
    assert reloaded(store).get("second").setup == AC_VOLTS


# --- rename and delete ---------------------------------------------------------------------------------------------


def test_a_renamed_preset_keeps_its_setup_and_its_place(store):
    store.save("First", DC_VOLTS)
    store.save("Second", AC_VOLTS)

    store.rename("First", "Bench supply")

    again = reloaded(store)
    assert again.names == ("Bench supply", "Second")
    assert again.get("Bench supply").setup == DC_VOLTS


def test_a_preset_can_be_renamed_to_a_different_case_of_its_own_name(store):
    store.save("bench", DC_VOLTS)

    store.rename("bench", "Bench")

    assert store.names == ("Bench",)


def test_a_preset_cannot_be_renamed_to_the_name_of_another(store):
    store.save("First", DC_VOLTS)
    store.save("Second", AC_VOLTS)

    with pytest.raises(PresetError, match="already a Preset called 'Second'"):
        store.rename("First", "second")

    assert reloaded(store).names == ("First", "Second")


def test_renaming_a_preset_that_is_not_there_is_an_error(store):
    with pytest.raises(PresetError, match="no Preset called 'Nope'"):
        store.rename("Nope", "Other")


def test_a_deleted_preset_is_gone_from_the_store_and_the_file(store):
    store.save("First", DC_VOLTS)
    store.save("Second", AC_VOLTS)

    store.delete("first")

    assert store.names == ("Second",)
    assert reloaded(store).names == ("Second",)


def test_deleting_a_preset_that_is_not_there_is_an_error(store):
    with pytest.raises(PresetError, match="no Preset called 'Nope'"):
        store.delete("Nope")


# --- the file on disk ----------------------------------------------------------------------------------------------


def test_a_write_that_fails_leaves_the_file_and_the_store_as_they_were(store, monkeypatch):
    store.save("First", DC_VOLTS)
    assert store.path is not None
    before = store.path.read_bytes()

    def refuse(*_: object) -> Path:
        reason = "read-only folder"
        raise PermissionError(reason)

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(PresetError, match=r"Could not write .*presets\.json.*read-only folder"):
        store.save("Second", AC_VOLTS)
    with pytest.raises(PresetError, match="Could not write"):
        store.delete("First")
    with pytest.raises(PresetError, match="Could not write"):
        store.rename("First", "Other")

    assert store.names == ("First",)
    assert store.path.read_bytes() == before
    assert stray_files(store.path.parent) == []  # no half-written temporary file is left behind


def test_a_store_is_written_through_a_temporary_file_that_is_not_left_behind(store):
    store.save("First", DC_VOLTS)
    store.save("Second", DC_VOLTS)

    assert store.path is not None
    assert stray_files(store.path.parent) == []


def test_a_missing_folder_is_created_when_the_first_preset_is_saved(tmp_path):
    store = PresetStore.load(tmp_path / "does" / "not" / "exist")

    store.save("First", DC_VOLTS)

    assert PresetStore.load(tmp_path / "does" / "not" / "exist").names == ("First",)


def test_a_file_that_is_not_json_gives_an_empty_store_a_problem_and_a_copy_of_the_file(tmp_path):
    (tmp_path / PRESETS_FILE).write_text("{ this is not json", encoding="utf-8")

    store = PresetStore.load(tmp_path)

    assert store.presets == ()
    assert len(store.problems) == 1
    assert "presets.json" in store.problems[0]
    assert (tmp_path / "presets.json.bad").read_text(encoding="utf-8") == "{ this is not json"
    store.save("Fresh", DC_VOLTS)  # the damaged file is replaced, because the copy keeps what was in it
    assert PresetStore.load(tmp_path).names == ("Fresh",)
    assert (tmp_path / "presets.json.bad").read_text(encoding="utf-8") == "{ this is not json"


def test_a_file_from_a_newer_version_is_not_used_and_not_lost(tmp_path):
    text = json.dumps({"format": "agilent34401a-presets", "version": FORMAT_VERSION + 1, "presets": []})
    (tmp_path / PRESETS_FILE).write_text(text, encoding="utf-8")

    store = PresetStore.load(tmp_path)

    assert store.presets == ()
    assert f"version {FORMAT_VERSION + 1}" in store.problems[0]
    assert (tmp_path / "presets.json.bad").read_text(encoding="utf-8") == text


def test_one_bad_preset_in_the_file_is_reported_and_the_others_still_load(store):
    store.save("Good", DC_VOLTS)
    store.save("Bad", AC_VOLTS)
    store.save("Also good", BURST)
    assert store.path is not None
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["presets"][1]["setup"]["range"] = 5.0
    document["presets"].append({"setup": document["presets"][0]["setup"]})  # no name
    document["presets"].append("nonsense")
    store.path.write_text(json.dumps(document), encoding="utf-8")

    again = reloaded(store)

    assert again.names == ("Good", "Also good")
    assert len(again.problems) == 3
    assert "Preset 'Bad'" in again.problems[0]
    assert "AC V has" in again.problems[0]
    assert "Preset 4" in again.problems[1]
    assert "name" in again.problems[1]
    assert "Preset 5" in again.problems[2]
    assert store.path.with_name("presets.json.bad").exists()


def test_a_name_that_appears_twice_in_the_file_keeps_the_first_and_reports_the_second(store):
    store.save("Same", DC_VOLTS)
    assert store.path is not None
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["presets"].append({"name": "SAME", "setup": document["presets"][0]["setup"]})
    store.path.write_text(json.dumps(document), encoding="utf-8")

    again = reloaded(store)

    assert again.names == ("Same",)
    assert "'SAME'" in again.problems[0]
    assert "already" in again.problems[0]


# --- export and import ---------------------------------------------------------------------------------------------


def test_the_whole_library_is_exported_to_a_file_and_imported_into_another_store(store, tmp_path):
    store.save("Bench supply", DC_VOLTS)
    store.save("Mains", AC_VOLTS)
    store.save("Burst of ohms", BURST)
    exported = tmp_path / "library.json"

    store.export_file(exported)
    other = PresetStore.in_memory()
    report = other.import_file(exported)

    assert other.names == ("Bench supply", "Mains", "Burst of ohms")
    assert other.get("Burst of ohms").setup == BURST
    assert report.added == ("Bench supply", "Mains", "Burst of ohms")
    assert report.problems == ()


def test_one_preset_can_be_exported_alone(store, tmp_path):
    store.save("Bench supply", DC_VOLTS)
    store.save("Mains", AC_VOLTS)
    exported = tmp_path / "mains.json"

    store.export_file(exported, ["mains"])

    other = PresetStore.in_memory()
    other.import_file(exported)
    assert other.names == ("Mains",)
    assert json.loads(exported.read_text(encoding="utf-8"))["version"] == FORMAT_VERSION


def test_exporting_a_preset_that_is_not_there_is_an_error_and_writes_nothing(store, tmp_path):
    exported = tmp_path / "nothing.json"

    with pytest.raises(PresetError, match="no Preset called 'Nope'"):
        store.export_file(exported, ["Nope"])

    assert not exported.exists()


def test_exporting_to_an_unwritable_place_is_an_error(store, tmp_path):
    store.save("Bench supply", DC_VOLTS)

    with pytest.raises(PresetError, match="Could not write"):
        store.export_file(tmp_path / "no-such-folder" / "out.json")


def test_importing_a_name_already_in_use_adds_it_under_a_new_name_by_default(store, tmp_path):
    store.save("Mains", AC_VOLTS)
    other = PresetStore.in_memory()
    other.save("Mains", DC_VOLTS)
    other.save("Mains (2)", BURST)
    exported = tmp_path / "other.json"
    other.export_file(exported)

    report = store.import_file(exported)

    assert store.names == ("Mains", "Mains (3)", "Mains (2)")
    assert store.get("Mains").setup == AC_VOLTS
    assert store.get("Mains (3)").setup == DC_VOLTS
    assert store.get("Mains (2)").setup == BURST
    assert report.renamed == (("Mains", "Mains (3)"),)
    assert report.added == ("Mains (3)", "Mains (2)")


def test_importing_can_replace_a_preset_of_the_same_name(store, tmp_path):
    store.save("Mains", AC_VOLTS)
    store.save("Other", DC_VOLTS)
    other = PresetStore.in_memory()
    other.save("MAINS", BURST)
    exported = tmp_path / "other.json"
    other.export_file(exported)

    report = store.import_file(exported, Collision.REPLACE)

    assert store.names == ("MAINS", "Other")
    assert store.get("Mains").setup == BURST
    assert report.replaced == ("MAINS",)
    assert report.added == ()


def test_importing_can_skip_a_preset_of_the_same_name(store, tmp_path):
    store.save("Mains", AC_VOLTS)
    other = PresetStore.in_memory()
    other.save("Mains", BURST)
    other.save("New one", DC_VOLTS)
    exported = tmp_path / "other.json"
    other.export_file(exported)

    report = store.import_file(exported, Collision.SKIP)

    assert store.names == ("Mains", "New one")
    assert store.get("Mains").setup == AC_VOLTS
    assert report.skipped == ("Mains",)
    assert report.added == ("New one",)


def test_the_names_an_import_would_collide_with_can_be_asked_for_first(store, tmp_path):
    store.save("Mains", AC_VOLTS)
    other = PresetStore.in_memory()
    other.save("mains", BURST)
    other.save("New one", DC_VOLTS)
    exported = tmp_path / "other.json"
    other.export_file(exported)

    assert store.collisions_with(exported) == ("mains",)
    assert store.names == ("Mains",)


def test_an_import_with_a_bad_preset_takes_the_good_ones_and_reports_the_bad(store, tmp_path):
    other = PresetStore.in_memory()
    other.save("Good", DC_VOLTS)
    other.save("Bad", AC_VOLTS)
    exported = tmp_path / "other.json"
    other.export_file(exported)
    document = json.loads(exported.read_text(encoding="utf-8"))
    document["presets"][1]["setup"]["function"] = "VOLT:XX"
    exported.write_text(json.dumps(document), encoding="utf-8")

    report = store.import_file(exported)

    assert store.names == ("Good",)
    assert report.added == ("Good",)
    assert len(report.problems) == 1
    assert "Preset 'Bad'" in report.problems[0]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json at all", "not valid JSON"),
        ("[1, 2]", "not a Preset file"),
        ('{"version": 1, "presets": []}', "not a Preset file"),
        ('{"format": "agilent34401a-presets", "version": 99, "presets": []}', "version 99"),
        ('{"format": "agilent34401a-presets", "version": 1}', "'presets'"),
        ('{"format": "agilent34401a-presets", "version": "one", "presets": []}', "version"),
    ],
)
def test_a_file_that_is_not_a_usable_presets_file_is_refused_whole(store, tmp_path, text, message):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(PresetError, match=message):
        store.import_file(path)

    assert store.presets == ()


def test_a_file_that_cannot_be_read_is_refused(store, tmp_path):
    with pytest.raises(PresetError, match=r"Could not read .*missing\.json"):
        store.import_file(tmp_path / "missing.json")
