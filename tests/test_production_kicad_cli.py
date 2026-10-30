"""Tests for Gerber/drill export through kicad-cli and the CLI's Gerber, JSON and error paths."""

import json
import os
import subprocess
import zipfile

import pytest

from kicad_jlcimport.production import kicad_cli as kc
from kicad_jlcimport.production.kicad_pcb import read_board_with_layers

from .test_production_kicad_pcb import BOARD_V8, _fake_caches

LAYERS_4 = """(kicad_pcb (version 20250114) (generator "pcbnew")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (6 "In2.Cu" power "GND")
    (4 "In1.Cu" signal)
    (9 "F.Adhes" user "F.Adhesive")
    (25 "Edge.Cuts" user)
  )
)
"""


class FakeKicadCli:
    """Stands in for ``subprocess.run`` on kicad-cli: writes what the real one would."""

    def __init__(self, gerber_rc=0, drill_rc=0, zone_refill=True, drill_files=("b-PTH.drl", "b-NPTH.drl")):
        self.calls = []
        self.gerber_rc, self.drill_rc = gerber_rc, drill_rc
        self.zone_refill = zone_refill
        self.drill_files = drill_files

    def __call__(self, args):
        self.calls.append(args)
        if "--help" in args:
            text = "  --check-zones  Check and refill zones if required" if self.zone_refill else "  --precision"
            return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
        out = args[args.index("--output") + 1]
        if args[3] == "gerbers":
            if self.gerber_rc:
                return subprocess.CompletedProcess(args, self.gerber_rc, stdout="", stderr="Failed to load board")
            names = ["b-F_Cu.gtl", "b-B_Cu.gbl", "b-job.gbrjob"]
        else:
            if self.drill_rc:
                return subprocess.CompletedProcess(args, self.drill_rc, stdout="", stderr="")
            names = list(self.drill_files) + ["b-drl_map.pdf"]
        for name in names:
            with open(os.path.join(out, name), "w") as f:
                f.write(name)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestCopperLayers:
    def test_layers_in_stack_order(self, tmp_path):
        path = tmp_path / "b.kicad_pcb"
        path.write_text(LAYERS_4)
        _records, layers = read_board_with_layers(str(path))
        assert layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_records_and_two_layer_default(self, tmp_path):
        path = tmp_path / "b.kicad_pcb"
        path.write_text(BOARD_V8)
        records, layers = read_board_with_layers(str(path))
        assert [r.reference for r in records] == ["R1", "D1", "H1"]
        assert layers == []
        assert kc.gerber_layers(layers)[:2] == ["F.Cu", "B.Cu"]


class TestCommands:
    def test_gerber_command(self):
        args = kc.gerber_command("kicad-cli", "b.kicad_pcb", "out", ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], True)
        layers = args[args.index("--layers") + 1].split(",")
        assert layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"] + kc.FIXED_LAYERS
        assert "--check-zones" in args and "--subtract-soldermask" in args
        assert "--use-drill-file-origin" not in args  # CPL uses the absolute origin
        assert args[-1] == "b.kicad_pcb"
        assert "--check-zones" not in kc.gerber_command("kicad-cli", "b.kicad_pcb", "out", [], False)

    def test_drill_command(self):
        args = kc.drill_command("kicad-cli", "b.kicad_pcb", "out")
        for flag, value in (("--excellon-units", "mm"), ("--drill-origin", "absolute"), ("--format", "excellon")):
            assert args[args.index(flag) + 1] == value
        assert "--excellon-separate-th" in args and "--generate-map" not in args

    def test_supports_zone_refill(self):
        assert kc.supports_zone_refill("kicad-cli", FakeKicadCli(zone_refill=True))
        assert not kc.supports_zone_refill("kicad-cli", FakeKicadCli(zone_refill=False))


class TestFindKicadCli:
    def test_explicit_path(self, tmp_path, monkeypatch):
        exe = tmp_path / "kicad-cli"
        exe.write_text("")
        monkeypatch.setenv("KICAD_CLI", "/nowhere/kicad-cli")
        assert kc.find_kicad_cli(str(exe)) == str(exe)

    def test_explicit_missing(self, monkeypatch):
        monkeypatch.setattr(kc.shutil, "which", lambda _name: None)
        assert kc.find_kicad_cli("/nowhere/kicad-cli") is None

    def test_env_then_path(self, tmp_path, monkeypatch):
        exe = tmp_path / "kicad-cli"
        exe.write_text("")
        monkeypatch.setenv("KICAD_CLI", str(exe))
        assert kc.find_kicad_cli() == str(exe)
        monkeypatch.delenv("KICAD_CLI")
        monkeypatch.setattr(kc.shutil, "which", lambda name: "/usr/bin/" + name)
        assert kc.find_kicad_cli() == "/usr/bin/kicad-cli"

    def test_windows_picks_newest(self, tmp_path, monkeypatch):
        for version in ("8.0", "10.0", "9.0"):
            (tmp_path / "KiCad" / version / "bin").mkdir(parents=True)
            (tmp_path / "KiCad" / version / "bin" / "kicad-cli.exe").write_text("")
        monkeypatch.delenv("KICAD_CLI", raising=False)
        monkeypatch.setattr(kc.shutil, "which", lambda _name: None)
        monkeypatch.setattr(kc.sys, "platform", "win32")
        monkeypatch.setattr(kc, "_WINDOWS_ROOTS", [str(tmp_path)])
        assert kc.find_kicad_cli() == str(tmp_path / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")

    def test_bad_env_path_is_not_replaced(self, monkeypatch):
        monkeypatch.setenv("KICAD_CLI", "/nowhere/kicad-cli")
        monkeypatch.setattr(kc.shutil, "which", lambda name: "/usr/bin/kicad-cli" if name == "kicad-cli" else None)
        assert kc.find_kicad_cli() is None
        assert "/nowhere/kicad-cli (from $KICAD_CLI)" in kc.not_found_message()
        assert kc.not_found_message("/opt/k") == "kicad-cli not found at /opt/k"
        monkeypatch.delenv("KICAD_CLI")
        assert "on PATH" in kc.not_found_message()

    def test_not_found(self, monkeypatch):
        monkeypatch.delenv("KICAD_CLI", raising=False)
        monkeypatch.setattr(kc.shutil, "which", lambda _name: None)
        monkeypatch.setattr(kc.sys, "platform", "linux")
        assert kc.find_kicad_cli() is None


class TestGenerate:
    def test_zip_has_gerbers_and_drills_only(self, tmp_path):
        fake = FakeKicadCli()
        zip_path, files = kc.generate_fabrication_zip("kicad-cli", "b.kicad_pcb", str(tmp_path), "b", [], run=fake)
        assert zip_path == str(tmp_path / "b-gerbers.zip")
        with zipfile.ZipFile(zip_path) as zf:
            assert sorted(zf.namelist()) == ["b-B_Cu.gbl", "b-F_Cu.gtl", "b-NPTH.drl", "b-PTH.drl"]
        assert sorted(os.listdir(tmp_path / "gerbers")) == sorted(os.path.basename(f) for f in files)
        assert sorted(os.listdir(tmp_path)) == ["b-gerbers.zip", "gerbers"]  # staging removed

    def test_gerber_failure(self, tmp_path):
        with pytest.raises(kc.KicadCliError, match="Failed to load board"):
            kc.generate_fabrication_zip("kicad-cli", "b.kicad_pcb", str(tmp_path), "b", [], run=FakeKicadCli(1))
        assert not (tmp_path / "b-gerbers.zip").exists()

    def test_drill_failure_leaves_output_untouched(self, tmp_path):
        (tmp_path / "gerbers").mkdir()
        (tmp_path / "gerbers" / "b-PTH.drl").write_text("previous run")
        with pytest.raises(kc.KicadCliError, match="exit 2"):
            kc.generate_fabrication_zip("kicad-cli", "b.kicad_pcb", str(tmp_path), "b", [], run=FakeKicadCli(0, 2))
        assert sorted(os.listdir(tmp_path)) == ["gerbers"]
        assert os.listdir(tmp_path / "gerbers") == ["b-PTH.drl"]  # no new Gerbers beside old drill files

    def test_run_failure_is_kicad_cli_error(self, tmp_path):
        not_executable = tmp_path / "kicad-cli"
        not_executable.write_text("")
        for path in (str(tmp_path / "missing"), str(not_executable)):
            with pytest.raises(kc.KicadCliError, match="Could not run"):
                kc._run([path, "version"])

    def test_no_drill_files(self, tmp_path):
        with pytest.raises(kc.KicadCliError, match="no drill files"):
            kc.generate_fabrication_zip(
                "kicad-cli", "b.kicad_pcb", str(tmp_path), "b", [], run=FakeKicadCli(drill_files=())
            )


class TestCliGerbers:
    def _setup(self, tmp_path, monkeypatch, fake, board_text=BOARD_V8, cli_path="/opt/kicad-cli"):
        import kicad_jlcimport.cli as cli
        from kicad_jlcimport.production import kicad_cli

        board = tmp_path / "proj" / "demo.kicad_pcb"
        board.parent.mkdir()
        board.write_text(board_text)
        monkeypatch.setattr(cli, "make_caches", _fake_caches)
        monkeypatch.setattr(cli, "find_kicad_cli", lambda _explicit=None: cli_path)
        monkeypatch.setattr(kicad_cli, "_run", fake)
        return cli, board

    def _main(self, cli, monkeypatch, argv):
        """Exit status of ``cli.main()`` (it returns normally on success)."""
        monkeypatch.setattr("sys.argv", ["prog"] + argv)
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code
        return 0

    def test_writes_gerbers_bom_and_cpl(self, tmp_path, monkeypatch, capsys):
        fake = FakeKicadCli()
        cli, board = self._setup(tmp_path, monkeypatch, fake)
        assert self._main(cli, monkeypatch, ["production", str(board)]) == 0
        out_dir = tmp_path / "proj" / "production"
        assert sorted(os.listdir(out_dir)) == [
            "demo-bom.csv",
            "demo-cpl.csv",
            "demo-gerbers.zip",
            "demo-report.txt",
            "gerbers",
        ]
        plot = next(c for c in fake.calls if c[3] == "gerbers" and "--help" not in c)
        assert "--check-zones" in plot and plot[-1] == str(board)
        assert "GERBERS" in capsys.readouterr().out

    def test_old_kicad_cli_plots_without_refill(self, tmp_path, monkeypatch, capsys):
        fake = FakeKicadCli(zone_refill=False)
        cli, board = self._setup(tmp_path, monkeypatch, fake)
        assert self._main(cli, monkeypatch, ["production", str(board)]) == 0
        plot = next(c for c in fake.calls if c[3] == "gerbers" and "--help" not in c)
        assert "--check-zones" not in plot
        assert "can't refill zones" in capsys.readouterr().out

    def test_no_refill_zones(self, tmp_path, monkeypatch):
        fake = FakeKicadCli()
        cli, board = self._setup(tmp_path, monkeypatch, fake)
        assert self._main(cli, monkeypatch, ["production", str(board), "--no-refill-zones"]) == 0
        assert all("--check-zones" not in c for c in fake.calls)
        assert all("--help" not in c for c in fake.calls)

    def test_plot_failure_writes_nothing(self, tmp_path, monkeypatch, capsys):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli(gerber_rc=3))
        assert self._main(cli, monkeypatch, ["production", str(board)]) == 1
        assert "Failed to load board" in capsys.readouterr().out
        assert not (tmp_path / "proj" / "production" / "demo-cpl.csv").exists()

    def test_missing_kicad_cli_fails_before_check(self, tmp_path, monkeypatch, capsys):
        import kicad_jlcimport.cli as cli

        def no_caches():
            raise AssertionError("the check must not run")

        cli_mod, board = self._setup(tmp_path, monkeypatch, FakeKicadCli(), cli_path=None)
        monkeypatch.setattr(cli, "make_caches", no_caches)
        assert self._main(cli_mod, monkeypatch, ["production", str(board)]) == 1
        out = capsys.readouterr().out
        assert "kicad-cli not found" in out and "--no-gerbers" in out

    def test_check_only_needs_no_kicad_cli(self, tmp_path, monkeypatch):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli(), cli_path=None)
        assert self._main(cli, monkeypatch, ["production", str(board), "--check-only"]) == 0

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli())
        assert self._main(cli, monkeypatch, ["production", str(board), "--json", "-n", "10"]) == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)  # stdout is only the JSON document
        assert "GERBERS" in captured.err
        assert data["board"] == str(board) and data["boards"] == 10 and data["errors"] == 0
        assert data["files"]["gerbers"].endswith("demo-gerbers.zip")
        assert [s["title"] for s in data["summary"]] == ["Order", "Fees", "Placements"]
        (part,) = data["parts"]
        assert part["lcsc"] == "C25804" and part["qty_needed"] == 10 and part["type"] == "Basic"
        assert part["url"] == "https://www.lcsc.com/product-detail/C25804.html"
        r1 = next(f for f in data["footprints"] if f["reference"] == "R1")
        assert r1["placement"]["rotation"] == 90 and r1["placement"]["confidence"] == "high"
        assert data["cpl"] == [
            {"Designator": "R1", "Mid X": "100.0000mm", "Mid Y": "-50.0000mm", "Rotation": "90", "Layer": "Top"}
        ]
        assert data["bom"][0]["JLCPCB Part #"] == "C25804"

    def test_json_check_only_errors(self, tmp_path, monkeypatch, capsys):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli(), board_text=BOARD_V8.replace("C25804", "C99999"))
        assert self._main(cli, monkeypatch, ["production", str(board), "--json", "--check-only"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["errors"] == 1 and data["files"] is None
        assert data["issues"][0] == {
            "severity": "error",
            "references": ["R1"],
            "message": "Not found in JLCPCB parts library",
        }

    def test_json_blocked_by_errors(self, tmp_path, monkeypatch, capsys):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli(), board_text=BOARD_V8.replace("C25804", "C99999"))
        assert self._main(cli, monkeypatch, ["production", str(board), "--json"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.out)["files"] is None
        assert "--force" in captured.err

    def test_probe_failure_is_reported(self, tmp_path, monkeypatch, capsys):
        def broken(args):
            raise kc.KicadCliError(f"Could not run {args[0]}: Permission denied")

        cli, board = self._setup(tmp_path, monkeypatch, broken)
        assert self._main(cli, monkeypatch, ["production", str(board), "--json"]) == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "Permission denied" in data["error"] and data["files"] is None and data["errors"] == 0
        assert "No files written" in captured.err

    def test_json_on_early_failures(self, tmp_path, monkeypatch, capsys):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli(), cli_path=None)
        assert self._main(cli, monkeypatch, ["production", str(tmp_path / "missing.kicad_pcb"), "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error"].startswith("could not read") and data["files"] is None
        assert self._main(cli, monkeypatch, ["production", str(board), "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert "kicad-cli not found" in data["error"]

    def test_output_dir_relative_to_cwd_and_kiprjmod(self, tmp_path, monkeypatch):
        cli, board = self._setup(tmp_path, monkeypatch, FakeKicadCli())
        monkeypatch.chdir(tmp_path)
        assert self._main(cli, monkeypatch, ["production", str(board), "--no-gerbers", "-o", "out"]) == 0
        assert (tmp_path / "out" / "demo-cpl.csv").exists()
        assert self._main(cli, monkeypatch, ["production", str(board), "--no-gerbers", "-o", "${KIPRJMOD}/fab"]) == 0
        assert (tmp_path / "proj" / "fab" / "demo-cpl.csv").exists()
