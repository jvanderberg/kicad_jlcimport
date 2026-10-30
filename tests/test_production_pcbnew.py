"""Tests for the pcbnew adapter using a fake pcbnew module."""

import os
import zipfile
from types import SimpleNamespace

import pytest

from kicad_jlcimport import production_pcbnew as adapter
from kicad_jlcimport.production import conventions as cv


class Vec:
    def __init__(self, x, y):
        self.x, self.y = x, y


def mm(v):
    return int(round(v * 1_000_000))


class FakePad:
    def __init__(self, number, x, y, use_name=False):
        self._number = number
        self._pos = Vec(mm(x), mm(y))
        if use_name:
            self.GetName = lambda: number
        else:
            self.GetNumber = lambda: number

    def GetPosition(self):
        return self._pos


class FakeField:
    def __init__(self, name, text):
        self._name, self._text = name, text

    def GetName(self):
        return self._name

    def GetText(self):
        return self._text


class FakeFootprint:
    def __init__(self, ref="R1", value="10k", x=10.0, y=20.0, rot=90.0, layer=0, pads=None, **api):
        self._ref, self._value = ref, value
        self._pos = Vec(mm(x), mm(y))
        self._rot = rot
        self._layer = layer
        self._pads = pads or [FakePad("1", 9.2, 20), FakePad("2", 10.8, 20)]
        for name, val in api.items():
            setattr(self, name, val)

    def GetReference(self):
        return self._ref

    def GetValue(self):
        return self._value

    def GetPosition(self):
        return self._pos

    def GetOrientationDegrees(self):
        return self._rot

    def GetLayer(self):
        return self._layer

    def GetFPID(self):
        return SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def Pads(self):
        return self._pads

    def GetAttributes(self):
        return getattr(self, "_attrs", 0)


def fake_pcbnew(**extra):
    mod = SimpleNamespace(
        F_Cu=0,
        B_Cu=2,
        In1_Cu=4,
        In2_Cu=6,
        F_Mask=10,
        B_Mask=11,
        F_Paste=12,
        B_Paste=13,
        F_SilkS=14,
        B_SilkS=15,
        Edge_Cuts=16,
        PLOT_FORMAT_GERBER=1,
        DRILL_MARKS_NO_DRILL_SHAPE=0,
        FP_DNP=0x40,
        FP_EXCLUDE_FROM_BOM=0x08,
        FP_EXCLUDE_FROM_POS_FILES=0x04,
    )
    for k, v in extra.items():
        setattr(mod, k, v)
    return mod


class TestExtract:
    def test_kicad8_style_footprint(self):
        fp = FakeFootprint(
            GetFieldsText=lambda: {"Reference": "R1", "Value": "10k", "LCSC": "C25804"},
            IsDNP=lambda: False,
            IsExcludedFromBOM=lambda: False,
            IsExcludedFromPosFiles=lambda: True,
            IsFlipped=lambda: False,
        )
        board = SimpleNamespace(GetFootprints=lambda: [fp])
        (rec,) = adapter.extract_footprints(board, fake_pcbnew())
        assert (rec.reference, rec.value, rec.footprint, rec.lcsc) == ("R1", "10k", "R_0603", "C25804")
        assert (rec.x, rec.y, rec.orientation, rec.side) == (10.0, 20.0, 90.0, cv.TOP)
        assert [(p.number, p.x, p.y) for p in rec.pads] == [("1", 9.2, 20.0), ("2", 10.8, 20.0)]
        assert rec.exclude_from_pos and not rec.dnp and not rec.exclude_from_bom

    def test_fields_list_and_attribute_bits(self):
        fp = FakeFootprint(
            layer=2,
            GetFields=lambda: [FakeField("Reference", "U1"), FakeField("JLCPCB Part #", "C1")],
            pads=[FakePad("1", 0, 0, use_name=True)],
        )
        fp._attrs = 0x40 | 0x08
        fp.GetOrientationDegrees = None  # KiCad builds without the degrees helper
        fp.GetOrientation = lambda: SimpleNamespace(AsDegrees=lambda: 45.0)
        rec = adapter.footprint_record(fp, fake_pcbnew())
        assert rec.side == cv.BOTTOM
        assert rec.orientation == 45.0
        assert rec.lcsc == "C1"
        assert rec.dnp and rec.exclude_from_bom and not rec.exclude_from_pos
        assert rec.pads[0].number == "1"

    def test_kicad7_properties_and_field_text_fallback(self):
        fp = FakeFootprint(GetProperties=lambda: {"Sim": "x"}, GetFieldText=lambda name: "C7" if name == "LCSC" else "")
        rec = adapter.footprint_record(fp, fake_pcbnew())
        assert rec.lcsc == "C7"
        assert rec.properties["Reference"] == "R1"

    def test_broken_getters_are_tolerated(self):
        def boom(*_a):
            raise RuntimeError("swig")

        fp = FakeFootprint(GetFieldsText=boom, GetFields=boom, GetProperties=boom, GetFieldText=boom, IsDNP=boom)
        fp.GetAttributes = boom
        rec = adapter.footprint_record(fp, fake_pcbnew())
        assert rec.lcsc is None and not rec.dnp

    def test_pad_pin_function(self):
        def boom():
            raise RuntimeError("swig")

        named, broken, plain = FakePad("1", 0, 0), FakePad("2", 1, 0), FakePad("3", 2, 0)
        named.GetPinFunction = lambda: "K"
        broken.GetPinFunction = boom
        rec = adapter.footprint_record(FakeFootprint(pads=[named, broken, plain]), fake_pcbnew())
        assert [p.function for p in rec.pads] == ["K", "", ""]

    def test_no_attribute_api(self):
        fp = FakeFootprint()
        rec = adapter.footprint_record(fp, SimpleNamespace(B_Cu=2))
        assert not rec.dnp

    def test_board_path(self):
        assert adapter.board_path(SimpleNamespace(GetFileName=lambda: "")) is None
        assert adapter.board_path(SimpleNamespace(GetFileName=lambda: "/x/b.kicad_pcb")) == "/x/b.kicad_pcb"


class FakePlotOptions:
    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        if name.startswith("Set") and name != "SetExcludeEdgeLayer":
            return lambda *a: self.calls.__setitem__(name, a)
        raise AttributeError(name)


class FakePlotController:
    def __init__(self, board, fail_on=None):
        self.opts = FakePlotOptions()
        self.layer = None
        self.plotted = []
        self.closed = False
        self._file = ""
        self._fail_on = fail_on

    def GetPlotOptions(self):
        return self.opts

    def SetLayer(self, layer):
        self.layer = layer

    def OpenPlotfile(self, suffix, fmt, desc):
        if suffix == self._fail_on:
            return False
        out = self.opts.calls["SetOutputDirectory"][0]
        self._file = os.path.join(out, f"b-{suffix}.gbr")
        with open(self._file, "w") as f:
            f.write(suffix)
        return True

    def GetPlotFileName(self):
        return self._file

    def PlotLayer(self):
        self.plotted.append(self.layer)

    def ClosePlot(self):
        self.closed = True


class FakeExcellon:
    def __init__(self, board):
        self.options = None

    def SetOptions(self, *a):
        self.options = a

    def SetFormat(self, metric):
        self.metric = metric

    def CreateDrillandMapFilesSet(self, out_dir, drill, gen_map):
        for name in ("b-PTH.drl", "b-NPTH.drl"):
            with open(os.path.join(out_dir, name), "w") as f:
                f.write("M48")


def plotting_pcbnew(controllers, **extra):
    def make_ctrl(board):
        ctrl = FakePlotController(board, **extra)
        controllers.append(ctrl)
        return ctrl

    filler = SimpleNamespace(Fill=lambda zones: controllers.append(("filled", zones)))
    return fake_pcbnew(
        PLOT_CONTROLLER=make_ctrl,
        EXCELLON_WRITER=FakeExcellon,
        VECTOR2I=lambda x, y: (x, y),
        ZONE_FILLER=lambda board: filler,
    )


def fake_board(copper=4):
    return SimpleNamespace(
        GetCopperLayerCount=lambda: copper, Zones=lambda: ["z"], GetFileName=lambda: "/p/b.kicad_pcb"
    )


class TestPlotting:
    def test_gerber_layers_include_inner_copper(self):
        layers = adapter.gerber_layers(fake_board(4), fake_pcbnew())
        suffixes = [s for _, s, _ in layers]
        assert suffixes[:4] == ["F_Cu", "B_Cu", "In1_Cu", "In2_Cu"]
        assert "Edge_Cuts" in suffixes and "F_Mask" in suffixes and "B_Paste" in suffixes
        assert len(adapter.gerber_layers(fake_board(2), fake_pcbnew())) == 9

    def test_generate_zip(self, tmp_path):
        controllers = []
        pcb = plotting_pcbnew(controllers)
        zip_path, files = adapter.generate_fabrication_zip(fake_board(4), str(tmp_path), "b", refill=True, pcbnew=pcb)
        assert controllers[0] == ("filled", ["z"])
        ctrl = controllers[1]
        assert ctrl.closed and len(ctrl.plotted) == 11
        assert ctrl.opts.calls["SetUseAuxOrigin"] == (False,)
        assert ctrl.opts.calls["SetDrillMarksType"] == (0,)
        assert zip_path == str(tmp_path / "b-gerbers.zip")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "b-F_Cu.gbr" in names and "b-PTH.drl" in names and "b-NPTH.drl" in names
        assert len(names) == 13 == len(files)

    def test_drill_marks_fallback_and_no_refill(self, tmp_path):
        controllers = []
        pcb = plotting_pcbnew(controllers)
        del pcb.DRILL_MARKS_NO_DRILL_SHAPE
        pcb.PCB_PLOT_PARAMS = SimpleNamespace(NO_DRILL_SHAPE=7)
        adapter.generate_fabrication_zip(fake_board(2), str(tmp_path), "b", refill=False, pcbnew=pcb)
        assert controllers[0].opts.calls["SetDrillMarksType"] == (7,)

    def test_open_plotfile_failure(self, tmp_path):
        pcb = plotting_pcbnew([], fail_on="B_Cu")
        with pytest.raises(RuntimeError, match="B_Cu"):
            adapter.plot_gerbers(fake_board(2), str(tmp_path), pcb)

    def test_try_call(self):
        def bad(*_a):
            raise TypeError("sig")

        obj = SimpleNamespace(Bad=bad, Good=lambda *a: None)
        assert adapter._try_call(obj, "Good", 1)
        assert not adapter._try_call(obj, "Bad", 1)
        assert not adapter._try_call(obj, "Missing")
