"""Extended tests for cli.py to improve coverage."""

import sys
from types import SimpleNamespace

from kicad_jlcimport import cli


class TestResolveProjectDir:
    """Tests for _resolve_project_dir helper."""

    def test_empty_path_returns_empty(self):
        assert cli._resolve_project_dir("") == ""

    def test_file_path_returns_parent_dir(self, tmp_path):
        test_file = tmp_path / "project.kicad_pro"
        test_file.write_text("")
        result = cli._resolve_project_dir(str(test_file))
        assert result == str(tmp_path)

    def test_dir_path_returns_dir(self, tmp_path):
        result = cli._resolve_project_dir(str(tmp_path))
        assert result == str(tmp_path)

    def test_relative_path_converted_to_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = cli._resolve_project_dir("subdir")
        assert result == str(subdir)


class TestCmdSearch:
    """Tests for cmd_search function."""

    def test_search_basic_type_filter(self, monkeypatch, capsys):
        mock_results = {
            "total": 2,
            "results": [
                {
                    "lcsc": "C1",
                    "type": "Basic",
                    "stock": 100,
                    "price": 0.01,
                    "model": "R1",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "Test",
                },
                {
                    "lcsc": "C2",
                    "type": "Extended",
                    "stock": 50,
                    "price": 0.02,
                    "model": "R2",
                    "package": "0603",
                    "brand": "ACME",
                    "description": "Test2",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="basic",
            min_stock=0,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "C1" in out
        assert "C2" not in out  # Extended filtered out

    def test_search_extended_type_filter(self, monkeypatch, capsys):
        mock_results = {
            "total": 2,
            "results": [
                {
                    "lcsc": "C1",
                    "type": "Basic",
                    "stock": 100,
                    "price": 0.01,
                    "model": "R1",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "Test",
                },
                {
                    "lcsc": "C2",
                    "type": "Extended",
                    "stock": 50,
                    "price": 0.02,
                    "model": "R2",
                    "package": "0603",
                    "brand": "ACME",
                    "description": "Test2",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="extended",
            min_stock=0,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "C2" in out
        assert "C1" not in out  # Basic filtered out

    def test_search_both_type_filter(self, monkeypatch, capsys):
        mock_results = {
            "total": 2,
            "results": [
                {
                    "lcsc": "C1",
                    "type": "Basic",
                    "stock": 100,
                    "price": 0.01,
                    "model": "R1",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "Test",
                },
                {
                    "lcsc": "C2",
                    "type": "Extended",
                    "stock": 50,
                    "price": 0.02,
                    "model": "R2",
                    "package": "0603",
                    "brand": "ACME",
                    "description": "Test2",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="both",
            min_stock=0,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "C1" in out
        assert "C2" in out  # Both included

    def test_search_min_stock_filter(self, monkeypatch, capsys):
        mock_results = {
            "total": 2,
            "results": [
                {
                    "lcsc": "C1",
                    "type": "Basic",
                    "stock": 100,
                    "price": 0.01,
                    "model": "R1",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "Test",
                },
                {
                    "lcsc": "C2",
                    "type": "Basic",
                    "stock": 5,
                    "price": 0.02,
                    "model": "R2",
                    "package": "0603",
                    "brand": "ACME",
                    "description": "Test2",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="both",
            min_stock=50,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "C1" in out
        assert "C2" not in out  # Low stock filtered

    def test_search_csv_output(self, monkeypatch, capsys):
        mock_results = {
            "total": 1,
            "results": [
                {
                    "lcsc": "C123",
                    "type": "Basic",
                    "stock": 100,
                    "price": 0.01,
                    "model": "R100K",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "100K Resistor",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="both",
            min_stock=0,
            csv=True,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "LCSC" in out  # CSV header
        assert "C123" in out
        assert "R100K" in out

    def test_search_no_results(self, monkeypatch, capsys):
        mock_results = {"total": 0, "results": []}
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="nonexistent",
            count=10,
            type="both",
            min_stock=0,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "No results found" in out

    def test_search_price_none_handling(self, monkeypatch, capsys):
        mock_results = {
            "total": 1,
            "results": [
                {
                    "lcsc": "C1",
                    "type": "Basic",
                    "stock": 100,
                    "price": None,
                    "model": "R1",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "Test",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="both",
            min_stock=0,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "N/A" in out  # Price is N/A

    def test_search_stock_none_handling(self, monkeypatch, capsys):
        mock_results = {
            "total": 1,
            "results": [
                {
                    "lcsc": "C1",
                    "type": "Basic",
                    "stock": None,
                    "price": 0.01,
                    "model": "R1",
                    "package": "0402",
                    "brand": "ACME",
                    "description": "Test",
                },
            ],
        }
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)

        args = SimpleNamespace(
            keyword="resistor",
            count=10,
            type="both",
            min_stock=0,
            csv=False,
        )
        cli.cmd_search(args)
        out = capsys.readouterr().out
        assert "N/A" in out  # Stock is N/A


class TestCmdImport:
    """Tests for cmd_import function."""

    def test_invalid_lcsc_id(self, capsys):
        args = SimpleNamespace(
            part="INVALID",
            show=None,
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="Test",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "Error" in out
        assert "Invalid LCSC part number" in out

    def test_invalid_project_path(self, capsys, monkeypatch):
        monkeypatch.setattr(cli, "validate_lcsc_id", lambda x: "C123")

        args = SimpleNamespace(
            part="C123",
            show=None,
            output=None,
            project="/nonexistent/path",
            global_dest=False,
            overwrite=False,
            lib_name="Test",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "Error" in out
        assert "does not exist" in out

    def test_import_output_only(self, tmp_path, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer

        fake_comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "",
            "datasheet": "",
            "manufacturer": "",
            "manufacturer_part": "",
            "footprint_data": {"dataStr": {"shape": ""}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

        class _Pad:
            layer = "1"

        class _Footprint:
            pads = [_Pad()]
            tracks = []
            model = None

        monkeypatch.setattr(importer, "fetch_full_component", lambda _lcsc: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *_a, **_k: _Footprint())
        monkeypatch.setattr(importer, "write_footprint", lambda *_a, **_k: "fp\n")

        args = SimpleNamespace(
            part="C123",
            show=None,
            output=str(tmp_path),
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "Saved:" in out
        assert (tmp_path / "TestPart.kicad_mod").exists()

    def test_import_no_destination_summary(self, tmp_path, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer

        fake_comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "",
            "datasheet": "",
            "manufacturer": "",
            "manufacturer_part": "",
            "footprint_data": {"dataStr": {"shape": ""}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

        class _Pad:
            layer = "1"

        class _Footprint:
            pads = [_Pad()]
            tracks = []
            model = None

        monkeypatch.setattr(importer, "fetch_full_component", lambda _lcsc: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *_a, **_k: _Footprint())
        monkeypatch.setattr(importer, "write_footprint", lambda *_a, **_k: "fp content\n")

        args = SimpleNamespace(
            part="C123",
            show=None,
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "Footprint:" in out
        assert "bytes" in out
        assert "Use --show" in out

    def test_import_show_footprint(self, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer

        fake_comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "",
            "datasheet": "",
            "manufacturer": "",
            "manufacturer_part": "",
            "footprint_data": {"dataStr": {"shape": ""}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

        class _Pad:
            layer = "1"

        class _Footprint:
            pads = [_Pad()]
            tracks = []
            model = None

        monkeypatch.setattr(importer, "fetch_full_component", lambda _lcsc: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *_a, **_k: _Footprint())
        monkeypatch.setattr(importer, "write_footprint", lambda *_a, **_k: "(footprint test)\n")

        args = SimpleNamespace(
            part="C123",
            show="footprint",
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "(footprint test)" in out

    def test_import_show_symbol(self, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer

        fake_comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "",
            "datasheet": "",
            "manufacturer": "",
            "manufacturer_part": "",
            "footprint_data": {"dataStr": {"shape": ""}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [{"dataStr": {"shape": ""}}],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

        class _Pad:
            layer = "1"

        class _Footprint:
            pads = [_Pad()]
            tracks = []
            model = None

        class _Symbol:
            pins = []
            rectangles = []

        monkeypatch.setattr(importer, "fetch_full_component", lambda _lcsc: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *_a, **_k: _Footprint())
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *_a, **_k: _Symbol())
        monkeypatch.setattr(importer, "write_footprint", lambda *_a, **_k: "fp\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *_a, **_k: "(symbol test)\n")

        args = SimpleNamespace(
            part="C123",
            show="symbol",
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "(symbol test)" in out

    def test_import_show_both(self, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer

        fake_comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "",
            "datasheet": "",
            "manufacturer": "",
            "manufacturer_part": "",
            "footprint_data": {"dataStr": {"shape": ""}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [{"dataStr": {"shape": ""}}],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

        class _Pad:
            layer = "1"

        class _Footprint:
            pads = [_Pad()]
            tracks = []
            model = None

        class _Symbol:
            pins = []
            rectangles = []

        monkeypatch.setattr(importer, "fetch_full_component", lambda _lcsc: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *_a, **_k: _Footprint())
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *_a, **_k: _Symbol())
        monkeypatch.setattr(importer, "write_footprint", lambda *_a, **_k: "(footprint test)\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *_a, **_k: "(symbol test)\n")

        args = SimpleNamespace(
            part="C123",
            show="both",
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "(footprint test)" in out
        assert "(symbol test)" in out

    def test_import_show_symbol_no_symbol_data(self, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer

        fake_comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "",
            "datasheet": "",
            "manufacturer": "",
            "manufacturer_part": "",
            "footprint_data": {"dataStr": {"shape": ""}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

        class _Pad:
            layer = "1"

        class _Footprint:
            pads = [_Pad()]
            tracks = []
            model = None

        monkeypatch.setattr(importer, "fetch_full_component", lambda _lcsc: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *_a, **_k: _Footprint())
        monkeypatch.setattr(importer, "write_footprint", lambda *_a, **_k: "fp\n")

        args = SimpleNamespace(
            part="C123",
            show="symbol",
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "No symbol data" in out

    def test_import_api_error(self, monkeypatch, capsys):
        import kicad_jlcimport.importer as importer
        from kicad_jlcimport.easyeda.api import APIError

        def raise_api_error(_):
            raise APIError("Component not found")

        monkeypatch.setattr(importer, "fetch_full_component", raise_api_error)

        args = SimpleNamespace(
            part="C123",
            show=None,
            output=None,
            project=None,
            global_dest=False,
            overwrite=False,
            lib_name="MyLib",
            kicad_version=9,
        )
        cli.cmd_import(args)
        out = capsys.readouterr().out
        assert "Error:" in out
        assert "Component not found" in out


class TestMain:
    """Tests for main function."""

    def test_main_no_command_shows_help(self, monkeypatch, capsys):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.save_config", lambda _: None)
        monkeypatch.setattr(sys, "argv", ["jlcimport"])
        cli.main()
        out = capsys.readouterr().out
        assert "JLCImport CLI" in out or "usage" in out.lower()

    def test_main_search_command(self, monkeypatch, capsys):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.save_config", lambda _: None)
        mock_results = {"total": 0, "results": []}
        monkeypatch.setattr(cli, "search_components", lambda *a, **k: mock_results)
        monkeypatch.setattr(sys, "argv", ["jlcimport", "search", "test"])
        cli.main()
        out = capsys.readouterr().out
        assert "No results found" in out
