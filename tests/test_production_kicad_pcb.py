"""Tests for the .kicad_pcb reader and the `production` CLI subcommand."""

import pytest

from kicad_jlcimport.production import conventions as cv
from kicad_jlcimport.production.cache import KeyedCache
from kicad_jlcimport.production.jlc_footprints import JlcFootprint
from kicad_jlcimport.production.kicad_pcb import parse_sexpr, read_board, read_board_text, tokenize
from kicad_jlcimport.production.models import PadRecord

BOARD_V8 = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 100 50 90)
    (property "Reference" "R1" (at 0 -1.43 90) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.43 90) (layer "F.Fab"))
    (property "LCSC" "C25804" (at 0 0 0) (layer "F.Fab") (hide yes))
    (attr smd)
    (pad "1" smd roundrect (at -0.8 0 90) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
    (pad "2" smd roundrect (at 0.8 0 90) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
  (footprint "Diode_SMD:D_SOD-123" (layer "B.Cu") (at 20 30 180)
    (property "Reference" "D1") (property "Value" "1N4148W") (property "JLCPCB Part #" "C81598")
    (attr smd dnp exclude_from_bom exclude_from_pos_files)
    (pad "1" smd rect (at -1.6 0.5 180) (size 0.9 1.2) (layers "B.Cu"))
    (pad "2" smd rect (at 1.6 0.5 180) (size 0.9 1.2) (layers "B.Cu"))
  )
  (footprint "MountingHole:MountingHole_3.2mm" (layer "F.Cu") (at 5 5)
    (property "Reference" "H1") (property "Value" "MountingHole")
    (dnp yes)
    (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask"))
  )
)
"""

BOARD_V7 = """(kicad_pcb (version 20221018) (generator pcbnew)
  (footprint "Lib:R" (layer "F.Cu") (at 1 2)
    (property "LCSC" "C1")
    (fp_text reference "R5" (at 0 0) (layer "F.SilkS"))
    (fp_text value "1k" (at 0 0) (layer "F.Fab"))
    (fp_text user "${REFERENCE}" (at 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))
  )
)
"""


class TestSexpr:
    def test_tokenize_strings_and_escapes(self):
        toks = list(tokenize('(a "b c" "q\\"x" "n\\nl" d)'))
        assert toks == ["(", "a", ("str", "b c"), ("str", 'q"x'), ("str", "n\nl"), "d", ")"]

    def test_parse_nested(self):
        assert parse_sexpr('(a (b 1 2) "(c)")') == ["a", ["b", "1", "2"], "(c)"]

    @pytest.mark.parametrize("text", ["(a (b)", "a)", ""])
    def test_unbalanced(self, text):
        with pytest.raises(ValueError):
            parse_sexpr(text)

    def test_not_a_board(self):
        with pytest.raises(ValueError):
            read_board_text("(footprint x)")


class TestReadBoard:
    def test_kicad8_board(self):
        r1, d1, h1 = read_board_text(BOARD_V8)

        assert (r1.reference, r1.value, r1.footprint, r1.lcsc) == ("R1", "10k", "R_0603_1608Metric", "C25804")
        assert (r1.x, r1.y, r1.orientation, r1.side) == (100.0, 50.0, 90.0, cv.TOP)
        # Rotated 90° CCW on screen: local -X pad moves down (+Y board).
        assert [(p.number, round(p.x, 6), round(p.y, 6)) for p in r1.pads] == [("1", 100.0, 50.8), ("2", 100.0, 49.2)]
        assert r1.in_cpl

        assert d1.side == cv.BOTTOM
        assert d1.lcsc == "C81598"
        assert d1.dnp and d1.exclude_from_bom and d1.exclude_from_pos
        assert [(round(p.x, 6), round(p.y, 6)) for p in d1.pads] == [(21.6, 29.5), (18.4, 29.5)]

        assert h1.dnp and h1.lcsc is None
        assert h1.pads[0].number == ""

    def test_kicad7_fp_text(self):
        (r5,) = read_board_text(BOARD_V7)
        assert (r5.reference, r5.value, r5.lcsc, r5.orientation) == ("R5", "1k", "C1", 0.0)

    def test_read_board_file(self, tmp_path):
        path = tmp_path / "b.kicad_pcb"
        path.write_text(BOARD_V7)
        assert read_board(str(path))[0].reference == "R5"


def _fake_caches():
    res = [PadRecord("1", -0.8, 0), PadRecord("2", 0.8, 0)]
    parts = {"C25804": {"lcsc": "C25804", "stock": 100, "type": "Basic", "price_breaks": [(1, 0.01)]}}
    return KeyedCache(parts.get), KeyedCache(lambda lcsc: JlcFootprint(lcsc, pads=res))


class TestCli:
    def _run(self, monkeypatch, argv):
        import kicad_jlcimport.cli as cli

        monkeypatch.setattr(cli, "make_caches", _fake_caches)
        # Don't depend on whether this machine has KiCad installed.
        monkeypatch.setattr(cli, "find_kicad_cli", lambda _explicit=None: None)
        monkeypatch.setattr("sys.argv", ["prog"] + argv)
        cli.main()

    def test_writes_bom_and_cpl(self, tmp_path, monkeypatch, capsys):
        board = tmp_path / "proj" / "demo.kicad_pcb"
        board.parent.mkdir()
        board.write_text(BOARD_V8)
        self._run(monkeypatch, ["production", str(board), "-n", "10", "--no-gerbers"])
        out = capsys.readouterr().out
        assert "JLCPCB production check" in out
        cpl = (tmp_path / "proj" / "production" / "demo-cpl.csv").read_text().splitlines()
        assert cpl == ["Designator,Mid X,Mid Y,Rotation,Layer", "R1,100.0000mm,-50.0000mm,90,Top"]
        bom = (tmp_path / "proj" / "production" / "demo-bom.csv").read_text().splitlines()
        assert bom[1:] == ["10k,R1,R_0603_1608Metric,C25804"]

    def test_check_only_exit_code(self, tmp_path, monkeypatch):
        board = tmp_path / "b.kicad_pcb"
        board.write_text(BOARD_V8.replace("C25804", "C99999"))
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, ["production", str(board), "--check-only"])
        assert exc.value.code == 1
        assert not (tmp_path / "production").exists()

    def test_check_only_success(self, tmp_path, monkeypatch):
        board = tmp_path / "b.kicad_pcb"
        board.write_text(BOARD_V8)
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, ["production", str(board), "--check-only"])
        assert exc.value.code == 0

    def test_no_stock_skips_parts_lookup(self, tmp_path, monkeypatch, capsys):
        import kicad_jlcimport.cli as cli

        def no_lookup_caches():
            def fail(_lcsc):
                raise AssertionError("parts library must not be queried")

            res = [PadRecord("1", -0.8, 0), PadRecord("2", 0.8, 0)]
            return KeyedCache(fail), KeyedCache(lambda lcsc: JlcFootprint(lcsc, pads=res))

        board = tmp_path / "b.kicad_pcb"
        board.write_text(BOARD_V8)
        monkeypatch.setattr(cli, "make_caches", no_lookup_caches)
        monkeypatch.setattr("sys.argv", ["prog", "production", str(board), "--no-stock", "--check-only"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert "Stock not checked" in capsys.readouterr().out

    def test_tls_error_prints_insecure_hint(self, tmp_path, monkeypatch, capsys):
        import kicad_jlcimport.cli as cli
        from kicad_jlcimport.easyeda.api import SSLCertError

        def tls_caches():
            def fail(_lcsc):
                raise SSLCertError("certificate verify failed")

            return KeyedCache(fail), KeyedCache(fail)

        board = tmp_path / "b.kicad_pcb"
        board.write_text(BOARD_V8)
        monkeypatch.setattr(cli, "make_caches", tls_caches)
        monkeypatch.setattr("sys.argv", ["prog", "production", str(board), "--check-only"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "--insecure" in capsys.readouterr().out

    def test_errors_block_without_force(self, tmp_path, monkeypatch, capsys):
        board = tmp_path / "b.kicad_pcb"
        board.write_text(BOARD_V8.replace("C25804", "C99999"))
        with pytest.raises(SystemExit):
            self._run(monkeypatch, ["production", str(board), "--no-gerbers"])
        assert "--force" in capsys.readouterr().out
        self._run(monkeypatch, ["production", str(board), "--force", "--no-gerbers", "-o", str(tmp_path / "out")])
        assert (tmp_path / "out" / "b-cpl.csv").exists()

    def test_bad_board(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            self._run(monkeypatch, ["production", str(tmp_path / "missing.kicad_pcb")])
        assert "could not read" in capsys.readouterr().out
