"""Global imports refer to the library through a KiCad path variable when one points at it."""

import json
import os

import pytest

from kicad_jlcimport import importer
from kicad_jlcimport.kicad import library


@pytest.fixture
def kicad_config(tmp_path, monkeypatch):
    """A fake KiCad config base. Returns a writer for ``<version>/kicad_common.json`` path variables."""
    base = tmp_path / "kicad-config"
    base.mkdir()
    monkeypatch.setattr(library, "_kicad_config_base", lambda: str(base))

    def write_vars(version, variables):
        ver_dir = base / version
        ver_dir.mkdir(exist_ok=True)
        (ver_dir / "kicad_common.json").write_text(json.dumps({"environment": {"vars": variables}}), encoding="utf-8")
        return ver_dir

    return write_vars


@pytest.fixture
def lib_dir(tmp_path):
    d = tmp_path / "shared" / "library"
    d.mkdir(parents=True)
    return str(d)


class TestFindPathVariable:
    def test_finds_variable_pointing_at_directory(self, kicad_config, lib_dir):
        kicad_config("10.0", {"OTHER": "/elsewhere", "SHARED_LIB": lib_dir})
        assert library.find_path_variable(lib_dir, kicad_version=10) == "SHARED_LIB"

    def test_ignores_trailing_separator_and_slash_style(self, kicad_config, lib_dir):
        kicad_config("10.0", {"SHARED_LIB": lib_dir.replace("\\", "/") + "/"})
        assert library.find_path_variable(lib_dir, kicad_version=10) == "SHARED_LIB"

    def test_no_match_returns_empty(self, kicad_config, lib_dir, tmp_path):
        kicad_config("10.0", {"SHARED_LIB": str(tmp_path / "somewhere-else")})
        assert library.find_path_variable(lib_dir, kicad_version=10) == ""

    def test_no_config_returns_empty(self, kicad_config, lib_dir):
        assert library.find_path_variable(lib_dir, kicad_version=10) == ""

    def test_prefers_target_version_config(self, kicad_config, lib_dir):
        kicad_config("10.0", {"FROM_TEN": lib_dir})
        kicad_config("9.0", {"FROM_NINE": lib_dir})
        assert library.find_path_variable(lib_dir, kicad_version=9) == "FROM_NINE"

    def test_falls_back_to_newest_readable_config(self, kicad_config, lib_dir):
        kicad_config("9.0", {"FROM_NINE": lib_dir})
        kicad_config("8.0", {"FROM_EIGHT": lib_dir})
        assert library.find_path_variable(lib_dir, kicad_version=10) == "FROM_NINE"

    def test_only_the_config_kicad_would_use_counts(self, kicad_config, lib_dir):
        """A variable that only an older KiCad defines must not be used: the current KiCad can't resolve it."""
        kicad_config("10.0", {"UNRELATED": "/elsewhere"})
        kicad_config("9.0", {"OLD_VAR": lib_dir})
        assert library.find_path_variable(lib_dir, kicad_version=10) == ""

    def test_skips_unreadable_config(self, kicad_config, lib_dir, tmp_path):
        ver_dir = tmp_path / "kicad-config" / "10.0"
        ver_dir.mkdir()
        (ver_dir / "kicad_common.json").write_text("{not json", encoding="utf-8")
        kicad_config("9.0", {"FROM_NINE": lib_dir})
        assert library.find_path_variable(lib_dir, kicad_version=10) == "FROM_NINE"

    @pytest.mark.parametrize(
        "content",
        [[], {"environment": None}, {"environment": {"vars": None}}, {"environment": {"vars": {"X": 5}}}],
    )
    def test_tolerates_unexpected_shapes(self, kicad_config, lib_dir, tmp_path, content):
        ver_dir = tmp_path / "kicad-config" / "10.0"
        ver_dir.mkdir()
        (ver_dir / "kicad_common.json").write_text(json.dumps(content), encoding="utf-8")
        assert library.find_path_variable(lib_dir, kicad_version=10) == ""

    def test_several_matches_pick_the_same_one_every_time(self, kicad_config, lib_dir):
        kicad_config("10.0", {"ZED": lib_dir, "ALPHA": lib_dir})
        assert library.find_path_variable(lib_dir, kicad_version=10) == "ALPHA"


class TestGlobalModelPath:
    def test_uses_path_variable(self, kicad_config, lib_dir):
        kicad_config("10.0", {"SHARED_LIB": lib_dir})
        path = importer._global_model_path(lib_dir, "shared", "PART", 10)
        assert path == "${SHARED_LIB}/shared.3dshapes/PART.wrl"

    def test_default_3rdparty_dir_still_uses_kicad_variable(self, kicad_config, tmp_path, monkeypatch):
        data_base = tmp_path / "kicad-data"
        monkeypatch.setattr(library, "_kicad_data_base", lambda: str(data_base))
        third_party = str(data_base / "10.0" / "3rdparty")
        kicad_config("10.0", {"MY_VAR": third_party})
        path = importer._global_model_path(third_party, "JLCImport", "PART", 10)
        assert path == "${KICAD10_3RD_PARTY}/JLCImport.3dshapes/PART.wrl"

    def test_falls_back_to_absolute_path(self, kicad_config, lib_dir):
        path = importer._global_model_path(lib_dir, "shared", "PART", 10)
        assert path == os.path.join(lib_dir, "shared.3dshapes", "PART.wrl").replace("\\", "/")
        assert "\\" not in path


class TestGlobalLibTables:
    def _uris(self, table):
        return [
            line.split('(uri "')[1].split('"')[0]
            for line in table.read_text(encoding="utf-8").splitlines()
            if "(uri" in line
        ]

    def test_tables_use_path_variable(self, kicad_config, lib_dir, tmp_path):
        kicad_config("10.0", {"SHARED_LIB": lib_dir})
        library.update_global_lib_tables(lib_dir, "shared", kicad_version=10)
        config_dir = tmp_path / "kicad-config" / "10.0"
        assert self._uris(config_dir / "sym-lib-table") == ["${SHARED_LIB}/shared.kicad_sym"]
        assert self._uris(config_dir / "fp-lib-table") == ["${SHARED_LIB}/shared.pretty"]

    def test_tables_use_absolute_path_without_variable(self, kicad_config, lib_dir, tmp_path):
        library.update_global_lib_tables(lib_dir, "shared", kicad_version=10)
        config_dir = tmp_path / "kicad-config" / "10.0"
        expected = lib_dir.replace("\\", "/")
        assert self._uris(config_dir / "sym-lib-table") == [f"{expected}/shared.kicad_sym"]
        assert self._uris(config_dir / "fp-lib-table") == [f"{expected}/shared.pretty"]
