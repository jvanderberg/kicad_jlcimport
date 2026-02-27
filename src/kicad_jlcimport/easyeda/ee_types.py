"""Dataclass types for parsed EasyEDA primitives."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class EEPad:
    shape: str  # "RECT", "OVAL", "ELLIPSE", "POLYGON"
    x: float
    y: float
    width: float
    height: float
    layer: str  # "1"=F.Cu, "2"=B.Cu, "11"=multilayer
    number: str
    drill: float  # drill diameter in mm (0 for SMD)
    rotation: float = 0.0
    polygon_points: List[float] = field(default_factory=list)
    slot_length: float = 0.0  # oval slot drill length in mm (0 for circular)


@dataclass
class EETrack:
    width: float
    layer: str
    points: List[Tuple[float, float]]


@dataclass
class EEArc:
    width: float
    layer: str
    start: Tuple[float, float]
    end: Tuple[float, float]
    rx: float
    ry: float
    large_arc: int
    sweep: int


@dataclass
class EECircle:
    cx: float
    cy: float
    radius: float
    width: float
    layer: str
    flag: str = ""  # EasyEDA flag field - "0" may indicate auxiliary circles
    filled: bool = False


@dataclass
class EERectangle:
    x: float
    y: float
    width: float
    height: float
    stroke_width: float = 0.0
    corner_radius: float = 0.0


@dataclass
class EEHole:
    x: float
    y: float
    radius: float


@dataclass
class EESolidRegion:
    layer: str
    points: List[Tuple[float, float]]
    region_type: str  # "npth", "solid", "cutout"


@dataclass
class EE3DModel:
    uuid: str
    origin_x: float
    origin_y: float
    z: float
    rotation: Tuple[float, float, float]


@dataclass
class EEPin:
    number: str
    name: str
    x: float
    y: float
    rotation: float
    length: float
    electrical_type: str
    name_visible: bool = True
    number_visible: bool = True


@dataclass
class EEPolyline:
    points: List[Tuple[float, float]]
    stroke_width: float = 0.0
    closed: bool = False
    fill: bool = False


@dataclass
class EEText:
    text: str
    x: float
    y: float
    rotation: float = 0.0
    font_size: float = 1.27  # mm


@dataclass
class EESymbol:
    rectangles: List[EERectangle] = field(default_factory=list)
    circles: List[EECircle] = field(default_factory=list)
    pins: List[EEPin] = field(default_factory=list)
    polylines: List[EEPolyline] = field(default_factory=list)
    arcs: List[EEArc] = field(default_factory=list)
    texts: List[EEText] = field(default_factory=list)


@dataclass
class EEFootprint:
    pads: List[EEPad] = field(default_factory=list)
    tracks: List[EETrack] = field(default_factory=list)
    arcs: List[EEArc] = field(default_factory=list)
    circles: List[EECircle] = field(default_factory=list)
    holes: List[EEHole] = field(default_factory=list)
    regions: List[EESolidRegion] = field(default_factory=list)
    model: Optional[EE3DModel] = None
