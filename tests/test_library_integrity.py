"""Regression tests for symbol-library integrity and crash-safe writes."""

import os
import re
import shutil
import sys

import pytest

from kicad_jlcimport.easyeda.ee_types import EEFootprint, EEPad, EEPin, EESymbol
from kicad_jlcimport.kicad import _fileio
from kicad_jlcimport.kicad._fileio import atomic_write
from kicad_jlcimport.kicad.footprint_writer import write_footprint
from kicad_jlcimport.kicad.library import (
    SymbolLibraryError,
    _scan_symbol_lib,
    add_symbol_to_lib,
    check_symbol_lib,
    symbol_exists,
)
from kicad_jlcimport.kicad.symbol_writer import write_symbol


def _top_level_names(text):
    symbols, _ = _scan_symbol_lib(text)
    return [name for name, _, _ in symbols]


def _balanced(text):
    return _scan_symbol_lib(text) is not None


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


SPACE_LIB = (
    "(kicad_symbol_lib\n"
    "  (version 20231120)\n"
    '  (generator "JLCImport")\n'
    '  (symbol "A"\n'
    '    (property "Description" "Op-amp (rail-to-rail")\n'
    '    (symbol "A_1_1"\n'
    "      (pin passive line)\n"
    "    )\n"
    "  )\n"
    '  (symbol "B"\n'
    '    (property "Description" "smiley :)")\n'
    "  )\n"
    '  (symbol "C"\n'
    "  )\n"
    ")\n"
)

TAB_LIB = (
    "(kicad_symbol_lib\n"
    "\t(version 20241209)\n"
    '\t(generator "kicad_symbol_editor")\n'
    '\t(symbol "A"\n'
    '\t\t(property "Reference" "U")\n'
    "\t)\n"
    '\t(symbol "B"\n'
    "\t)\n"
    ")\n"
)

NEW_A = '  (symbol "A"\n    (property "Reference" "U2")\n  )\n'


class TestScanSymbolLib:
    def test_reports_only_top_level_symbols(self):
        assert _top_level_names(SPACE_LIB) == ["A", "B", "C"]

    def test_ignores_parens_inside_strings(self):
        symbols, root_close = _scan_symbol_lib(SPACE_LIB)
        a_name, a_start, a_end = symbols[0]
        assert SPACE_LIB[a_start:a_end].endswith("    )\n  )")
        assert root_close == len(SPACE_LIB) - 2

    def test_escaped_quotes(self):
        text = '(kicad_symbol_lib\n  (symbol "Q\\"1"\n    (property "D" "say \\"hi\\" ((")\n  )\n)\n'
        assert _top_level_names(text) == ['Q"1']

    def test_single_line_file(self):
        text = '(kicad_symbol_lib (version 1) (symbol "A" (pin)) (symbol "B"))'
        assert _top_level_names(text) == ["A", "B"]

    @pytest.mark.parametrize(
        "text",
        [
            "(kicad_symbol_lib\n",
            '(kicad_symbol_lib (symbol "A" (property "x" "unterminated)))\n',
            "(kicad_symbol_lib))\n",
        ],
    )
    def test_malformed_returns_none(self, text):
        assert _scan_symbol_lib(text) is None


class TestOverwriteKeepsOtherSymbols:
    def test_unbalanced_paren_in_description(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        assert add_symbol_to_lib(sym_path, "A", NEW_A, overwrite=True) is True
        text = _read(sym_path)
        assert _top_level_names(text) == ["A", "B", "C"]
        assert '"U2"' in text
        assert "rail-to-rail" not in text
        assert "A_1_1" not in text

    def test_stray_close_paren_in_description(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        new_b = '  (symbol "B"\n    (property "Description" "new")\n  )\n'
        assert add_symbol_to_lib(sym_path, "B", new_b, overwrite=True) is True
        text = _read(sym_path)
        assert _top_level_names(text) == ["A", "B", "C"]
        assert "smiley" not in text

    def test_tab_indented_library_replaces_instead_of_duplicating(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, TAB_LIB)
        assert add_symbol_to_lib(sym_path, "A", NEW_A, overwrite=True) is True
        text = _read(sym_path)
        assert _top_level_names(text) == ["A", "B"]
        assert '"U2"' in text
        assert '(property "Reference" "U")' not in text

    def test_replacement_keeps_position(self, tmp_path):
        # Parents must precede derived (extends) symbols, so replace in place.
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        add_symbol_to_lib(sym_path, "A", NEW_A, overwrite=True)
        assert _top_level_names(_read(sym_path))[0] == "A"

    def test_existing_duplicates_are_collapsed(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, '(kicad_symbol_lib\n  (symbol "A"\n  )\n  (symbol "B"\n  )\n  (symbol "A"\n  )\n)\n')
        add_symbol_to_lib(sym_path, "A", NEW_A, overwrite=True)
        assert _top_level_names(_read(sym_path)) == ["A", "B"]

    @pytest.mark.parametrize("name", ["A", "D"])
    def test_crlf_line_endings_preserved(self, tmp_path, name):
        sym_path = str(tmp_path / "lib.kicad_sym")
        with open(sym_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(SPACE_LIB)
        new = NEW_A.replace('"A"', f'"{name}"')
        assert add_symbol_to_lib(sym_path, name, new, overwrite=True) is True
        with open(sym_path, "rb") as f:
            raw = f.read()
        assert raw.count(b"\n") == raw.count(b"\r\n")
        assert _top_level_names(raw.decode("utf-8")) == (["A", "B", "C"] if name == "A" else ["A", "B", "C", "D"])
        assert b'"U2")\r\n' in raw

    def test_lf_file_stays_lf(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        add_symbol_to_lib(sym_path, "D", '  (symbol "D"\n  )\n')
        with open(sym_path, "rb") as f:
            assert b"\r" not in f.read()

    def test_repeated_overwrites_stay_well_formed(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        for _ in range(3):
            for name in ("A", "B", "C", "D"):
                content = f'  (symbol "{name}"\n    (property "Description" "x (y")\n  )\n'
                add_symbol_to_lib(sym_path, name, content, overwrite=True)
        text = _read(sym_path)
        assert _balanced(text)
        assert _top_level_names(text) == ["A", "B", "C", "D"]


class TestUnitSubSymbolNames:
    def test_unit_name_is_not_an_existing_symbol(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        content = '  (symbol "A_1_1"\n  )\n'
        assert add_symbol_to_lib(sym_path, "A_1_1", content) is True
        assert _top_level_names(_read(sym_path)) == ["A", "B", "C", "A_1_1"]

    def test_overwriting_unit_name_leaves_parent_intact(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        add_symbol_to_lib(sym_path, "A_1_1", '  (symbol "A_1_1"\n  )\n', overwrite=True)
        text = _read(sym_path)
        assert "(pin passive line)" in text
        assert _top_level_names(text) == ["A", "B", "C", "A_1_1"]


class TestMalformedLibraryUntouched:
    @pytest.mark.parametrize(
        "original",
        [
            '(kicad_symbol_lib\n  (symbol "A"\n)\n',
            '(kicad_symbol_lib\n  (symbol "A")\n)\n\x00\x00\x00',
            '(kicad_symbol_lib\n  (symbol "A" (property "x" "open))\n)\n',
        ],
    )
    def test_raises_and_leaves_file_unchanged(self, tmp_path, original):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, original)
        with pytest.raises(SymbolLibraryError, match="left unchanged"):
            add_symbol_to_lib(sym_path, "B", '  (symbol "B")\n')
        with pytest.raises(SymbolLibraryError):
            check_symbol_lib(sym_path)
        assert _read(sym_path) == original

    def test_check_accepts_missing_and_valid_files(self, tmp_path):
        check_symbol_lib(str(tmp_path / "missing.kicad_sym"))
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        check_symbol_lib(sym_path)


class TestAtomicWrite:
    def test_failure_mid_write_leaves_original(self, tmp_path, monkeypatch):
        path = str(tmp_path / "lib.kicad_sym")
        _write(path, SPACE_LIB)

        def fail_fsync(fd):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(_fileio.os, "fsync", fail_fsync)
        with pytest.raises(OSError):
            add_symbol_to_lib(path, "D", '  (symbol "D")\n')
        assert _read(path) == SPACE_LIB
        assert os.listdir(str(tmp_path)) == ["lib.kicad_sym"]

    def test_writes_text_and_bytes(self, tmp_path):
        text_path = str(tmp_path / "a.txt")
        bin_path = str(tmp_path / "b.bin")
        atomic_write(text_path, "10kΩ ±1% 0.1µF\n")
        atomic_write(bin_path, b"\x00\x01")
        assert _read(text_path) == "10kΩ ±1% 0.1µF\n"
        with open(bin_path, "rb") as f:
            assert f.read() == b"\x00\x01"

    def test_preserves_newlines_verbatim(self, tmp_path):
        path = str(tmp_path / "a.txt")
        atomic_write(path, "a\nb\n")
        with open(path, "rb") as f:
            assert f.read() == b"a\nb\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_preserves_existing_permissions(self, tmp_path):
        path = str(tmp_path / "a.txt")
        _write(path, "old")
        os.chmod(path, 0o640)
        atomic_write(path, "new")
        assert os.stat(path).st_mode & 0o777 == 0o640

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_new_file_respects_umask(self, tmp_path):
        path = str(tmp_path / "a.txt")
        old = os.umask(0o022)
        try:
            atomic_write(path, "x")
        finally:
            os.umask(old)
        assert os.stat(path).st_mode & 0o777 == 0o644


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file semantics")


class TestAtomicWriteFallbacks:
    def test_umask_is_not_touched(self, tmp_path, monkeypatch):
        def no_umask(_mask):
            raise AssertionError("os.umask is process-wide and must not be called")

        monkeypatch.setattr(_fileio.os, "umask", no_umask)
        atomic_write(str(tmp_path / "new.txt"), "x")
        atomic_write(str(tmp_path / "new.txt"), "y")

    @posix_only
    def test_symlink_is_followed(self, tmp_path):
        real = tmp_path / "shared" / "lib.kicad_sym"
        real.parent.mkdir()
        _write(str(real), SPACE_LIB)
        link = tmp_path / "project" / "lib.kicad_sym"
        link.parent.mkdir()
        link.symlink_to(real)

        assert add_symbol_to_lib(str(link), "D", '  (symbol "D"\n  )\n') is True

        assert link.is_symlink()
        assert _top_level_names(_read(str(real))) == ["A", "B", "C", "D"]
        assert os.listdir(str(link.parent)) == ["lib.kicad_sym"]

    @posix_only
    def test_hard_link_is_preserved(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        _write(str(a), "old")
        os.link(str(a), str(b))
        atomic_write(str(a), "new")
        assert _read(str(b)) == "new"
        assert os.path.samefile(str(a), str(b))

    @posix_only
    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
    def test_read_only_directory_writes_in_place(self, tmp_path):
        directory = tmp_path / "locked"
        directory.mkdir()
        path = directory / "lib.kicad_sym"
        _write(str(path), "old")
        os.chmod(str(directory), 0o555)
        try:
            atomic_write(str(path), "new")
        finally:
            os.chmod(str(directory), 0o755)
        assert _read(str(path)) == "new"

    @posix_only
    def test_file_owned_by_another_user_is_written_in_place(self, tmp_path, monkeypatch):
        path = tmp_path / "a.txt"
        _write(str(path), "old")
        inode = os.stat(str(path)).st_ino
        monkeypatch.setattr(_fileio.os, "geteuid", lambda: os.stat(str(path)).st_uid + 1)
        atomic_write(str(path), "new")
        assert _read(str(path)) == "new"
        assert os.stat(str(path)).st_ino == inode

    @posix_only
    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can chown to any group")
    def test_group_that_cannot_be_kept_is_written_in_place(self, tmp_path):
        tmp = tmp_path / "t"
        _write(str(tmp), "")
        st = os.stat(str(tmp))
        fields = list(st)
        fields[5] = max(os.getgroups() + [st.st_gid]) + 12345  # a group we are not in
        assert _fileio._copy_metadata(os.stat_result(fields), str(tmp)) is False

    def test_locked_target_retries_then_writes_in_place(self, tmp_path, monkeypatch):
        path = tmp_path / "a.txt"
        _write(str(path), "old")
        calls = []

        def locked(src, dst):
            calls.append(src)
            raise PermissionError(13, "The process cannot access the file")

        monkeypatch.setattr(_fileio, "_REPLACE_RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(_fileio.os, "replace", locked)
        atomic_write(str(path), "new")
        assert len(calls) == 3
        assert _read(str(path)) == "new"
        assert os.listdir(str(tmp_path)) == ["a.txt"]

    def test_transient_lock_still_replaces_atomically(self, tmp_path, monkeypatch):
        path = tmp_path / "a.txt"
        _write(str(path), "old")
        real_replace = os.replace
        attempts = []

        def flaky(src, dst):
            attempts.append(src)
            if len(attempts) == 1:
                raise PermissionError(13, "locked")
            real_replace(src, dst)

        monkeypatch.setattr(_fileio, "_REPLACE_RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(_fileio.os, "replace", flaky)
        atomic_write(str(path), "new")
        assert len(attempts) == 2
        assert _read(str(path)) == "new"

    def test_chmod_failure_is_ignored(self, tmp_path, monkeypatch):
        path = tmp_path / "a.txt"
        _write(str(path), "old")

        def no_chmod(*_a, **_k):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(_fileio.os, "chmod", no_chmod)
        atomic_write(str(path), "new")
        assert _read(str(path)) == "new"


class TestAtomicWriteProtections:
    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
    def test_read_only_file_is_refused(self, tmp_path):
        path = str(tmp_path / "lib.kicad_sym")
        _write(path, SPACE_LIB)
        os.chmod(path, 0o444)
        try:
            with pytest.raises(PermissionError):
                add_symbol_to_lib(path, "D", '  (symbol "D")\n')
            assert _read(path) == SPACE_LIB
            assert os.listdir(str(tmp_path)) == ["lib.kicad_sym"]
        finally:
            os.chmod(path, 0o644)

    def test_in_place_write_checks_free_space_first(self, tmp_path, monkeypatch):
        path = str(tmp_path / "a.txt")
        _write(path, "original contents")
        monkeypatch.setattr(_fileio, "_can_replace", lambda st: False)
        usage = shutil.disk_usage(str(tmp_path))._replace(free=3)
        monkeypatch.setattr(_fileio.shutil, "disk_usage", lambda d: usage)
        with pytest.raises(OSError, match="free space"):
            atomic_write(path, "new contents")
        assert _read(path) == "original contents"

    @pytest.mark.parametrize("new", ["short", "a much longer replacement than before"])
    def test_in_place_write_replaces_contents_exactly(self, tmp_path, monkeypatch, new):
        path = str(tmp_path / "a.txt")
        _write(path, "original contents")
        inode = os.stat(path).st_ino
        monkeypatch.setattr(_fileio, "_can_replace", lambda st: False)
        atomic_write(path, new)
        assert _read(path) == new
        assert os.stat(path).st_ino == inode

    def test_group_that_cannot_be_kept_falls_back_to_in_place(self, tmp_path, monkeypatch):
        path = str(tmp_path / "a.txt")
        _write(path, "old")
        inode = os.stat(path).st_ino
        monkeypatch.setattr(_fileio, "_copy_metadata", lambda st, tmp: False)
        atomic_write(path, "new")
        assert _read(path) == "new"
        assert os.stat(path).st_ino == inode
        assert os.listdir(str(tmp_path)) == ["a.txt"]


class TestSymbolExists:
    def test_matches_only_top_level_symbols(self, tmp_path):
        path = str(tmp_path / "lib.kicad_sym")
        _write(path, SPACE_LIB)
        assert symbol_exists(path, "A")
        assert not symbol_exists(path, "A_1_1")
        assert not symbol_exists(str(tmp_path / "missing.kicad_sym"), "A")

    def test_check_symbol_lib_returns_names(self, tmp_path):
        path = str(tmp_path / "lib.kicad_sym")
        _write(path, SPACE_LIB)
        assert check_symbol_lib(path) == {"A", "B", "C"}
        assert check_symbol_lib(str(tmp_path / "missing.kicad_sym")) == set()


def test_no_temp_files_left_after_import(tmp_path):
    sym_path = str(tmp_path / "lib.kicad_sym")
    add_symbol_to_lib(sym_path, "A", NEW_A)
    add_symbol_to_lib(sym_path, "A", NEW_A, overwrite=True)
    assert [p for p in os.listdir(str(tmp_path)) if re.search(r"\.tmp$", p)] == []


HOSTILE = 'x"y\\z(q)'


def _kicad_strings(text):
    """Return every quoted string in *text*, unescaped the way KiCad reads them."""
    return [re.sub(r"\\(.)", r"\1", m) for m in re.findall(r'"((?:[^"\\]|\\.)*)"', text)]


class TestWritersEscapeNetworkData:
    def test_symbol_fields_with_quotes_and_backslashes(self, tmp_path):
        sym = EESymbol()
        sym.pins = [EEPin(number=HOSTILE, name="IN", x=0, y=0, rotation=0, length=2.54, electrical_type="input")]
        content = write_symbol(
            sym,
            "Part",
            prefix=HOSTILE,
            footprint_ref=HOSTILE,
            lcsc_id=HOSTILE,
            datasheet=HOSTILE,
        )
        assert _kicad_strings(content).count(HOSTILE) == 5

        sym_path = str(tmp_path / "lib.kicad_sym")
        assert add_symbol_to_lib(sym_path, "Part", content) is True
        assert add_symbol_to_lib(sym_path, "Other", content.replace('"Part', '"Other')) is True
        assert check_symbol_lib(sym_path) == {"Part", "Other"}

    def test_footprint_fields_with_quotes_and_backslashes(self):
        fp = EEFootprint()
        fp.pads.append(EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number=HOSTILE, drill=0, rotation=0))
        text = write_footprint(fp, "Part", lcsc_id=HOSTILE, datasheet=HOSTILE, model_path=HOSTILE)
        assert _scan_symbol_lib(text) is not None
        assert _kicad_strings(text).count(HOSTILE) == 4


class TestGeneratedSymbolGuard:
    BAD = '  (symbol "Part"\n    (property "Datasheet" "a"b")\n  )\n'

    def test_malformed_symbol_is_not_appended(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        with pytest.raises(SymbolLibraryError, match="generated symbol 'Part' is malformed"):
            add_symbol_to_lib(sym_path, "Part", self.BAD)
        assert _read(sym_path) == SPACE_LIB

    def test_malformed_symbol_does_not_create_library(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        with pytest.raises(SymbolLibraryError):
            add_symbol_to_lib(sym_path, "Part", self.BAD)
        assert not os.path.exists(sym_path)

    def test_content_must_contain_the_named_symbol(self, tmp_path):
        sym_path = str(tmp_path / "lib.kicad_sym")
        _write(sym_path, SPACE_LIB)
        with pytest.raises(SymbolLibraryError):
            add_symbol_to_lib(sym_path, "Part", '  (symbol "Wrong")\n')
        assert _read(sym_path) == SPACE_LIB


class TestUnreadableLibrary:
    def test_non_utf8_library_gives_clean_error(self, tmp_path):
        sym_path = tmp_path / "lib.kicad_sym"
        original = '(kicad_symbol_lib\n  (symbol "Caf\xe9")\n)\n'.encode("latin-1")
        sym_path.write_bytes(original)
        with pytest.raises(SymbolLibraryError, match="not UTF-8"):
            check_symbol_lib(str(sym_path))
        with pytest.raises(SymbolLibraryError, match="not UTF-8"):
            add_symbol_to_lib(str(sym_path), "B", '  (symbol "B")\n')
        assert sym_path.read_bytes() == original

    @posix_only
    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
    def test_unreadable_library_gives_clean_error(self, tmp_path):
        sym_path = tmp_path / "lib.kicad_sym"
        sym_path.write_text(SPACE_LIB)
        os.chmod(str(sym_path), 0)
        try:
            with pytest.raises(SymbolLibraryError, match="could not be read"):
                check_symbol_lib(str(sym_path))
        finally:
            os.chmod(str(sym_path), 0o644)
