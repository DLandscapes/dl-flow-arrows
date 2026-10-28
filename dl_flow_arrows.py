# SPDX-FileCopyrightText: 2026 Marc Ihle (Digital Landscapes)
# SPDX-License-Identifier: GPL-3.0-or-later
"""DL Flow Arrows - a QGIS Processing script by Digital Landscapes (https://digital-landscapes.com).

Version 1.0.0 (2026-10-28). Written for QGIS 3.44 LTR. https://github.com/DLandscapes/dl-flow-arrows

Copyright 2026 Marc Ihle (Digital Landscapes). This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
General Public License for more details: https://www.gnu.org/licenses/.

A terrain (DTM) in, a point layer out: one point per cell (or per block of cells), with the attributes that say where
water would flow from there - drawn as arrows by ONE standard element (a Simple Marker "Arrow", rotated by 'azimuth',
sized by 'length'), so it can be restyled like any layer, with the assistant if you like. Two meanings:
- Fall line (default): the direction of steepest descent, from QGIS's own native:aspect and native:slope (Horn's 3x3
  formula - measured 2026-10-27: 0.000 deg difference). Continuous; it points INTO hollows, where water collects.
- Flow direction D8: the one of the 8 neighbours water passes to, on the SINK-FILLED terrain, from QGIS's own
  native:fillsinkswangliu (Wang & Liu 2006; QGIS's port of the SAGA GIS tool). 45-degree steps; across a filled
  hollow it follows the spill route, not the slope - the field fill_m says how much the fill raised the cell.
Catchment area (default on): the total upslope area draining through each cell (m2), sampled at the point - computed
here on QGIS's own sink-filled terrain, the same way as SAGA GIS "Flow Accumulation (Top-Down)" with its default
settings (multiple flow direction, convergence 1.1, cell area); D8 as an option.
Optional: the same arrows as simple lines (one straight segment each, drawn downhill) for DXF / Blender.
The directions are QGIS's; this script adds the sampling (density), the catchment, the attributes, the geometry and
the style. The version history is in CHANGELOG.md.

Install: Processing Toolbox > Python icon > "Add Script to Toolbox..." and pick this file.
"""
import math
import warnings

import numpy as np
import processing
from osgeo import gdal
from qgis.core import (
    Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsLineSymbol, QgsMarkerSymbol, QgsPointXY,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingLayerPostProcessorInterface,
    QgsProcessingMultiStepFeedback, QgsProcessingOutputNumber, QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer, QgsProperty, QgsSimpleLineSymbolLayer, QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor

VERSION = "1.0.0"

MFD_CONVERGENCE = 1.1  # SAGA ta_hydrology 0 default (saga_cmd 9.12.4 help)
LENGTH_SHARE = 0.8  # arrow length = this share of the spacing between arrows
MIN_LENGTH_SHARE = 0.15  # 'longer where steeper': the shortest arrow, as a share of the full length
MANY = 500_000  # warn above this many arrows (drawing gets slow)
INK = QColor(38, 36, 31)  # DL ink

METHODS = ["Fall line (steepest slope)", "Flow direction D8 (after filling sinks)"]
SAMPLES = ["Average of the block", "The cell under the arrow"]
LENGTHS = ["Fixed", "Longer where steeper"]
ROUTINGS = [f"Multiple flow direction (SAGA's default, convergence {MFD_CONVERGENCE})", "Single flow direction (D8)"]


def arrow_fields(d8=False, catchment=True):
    """Only the fields that carry values for this run (0.2.x left d8_code / fill_m blank for the fall line)."""
    spec = [("azimuth", QMetaType.Type.Double), ("slope_deg", QMetaType.Type.Double), ("length", QMetaType.Type.Double),
            ("method", QMetaType.Type.QString), ("cells", QMetaType.Type.Int)]
    if catchment:
        spec.append(("catch_m2", QMetaType.Type.Double))
    if d8:
        spec += [("d8_code", QMetaType.Type.Int), ("fill_m", QMetaType.Type.Double)]
    fields = QgsFields()
    for name, kind in spec:
        fields.append(QgsField(name, kind))
    return fields


def catchment_area(z, dx, dy, mfd=True, convergence=MFD_CONVERGENCE, return_exit=False):
    """Total catchment area (m2) of every cell: its own area plus all area draining into it, routed over the terrain z
    (a sink-filled DEM, NaN = no data) - matched to SAGA GIS "Flow Accumulation (Top-Down)" (ta_hydrology 0,
    -FLOW_UNIT 1; behaviour read in SAGA's Flow_Parallel.cpp - "O.Conrad (c) 2001-2016, Portions by T.Grabs (c) 2010"
    as saga_cmd names the tool's authors - re-implemented here, not copied):
    - MFD (METHOD 4, contour length off): every lower neighbour gets the share (drop / distance) ** convergence. For a
      neighbour outside the grid or without data, the drop is the OPPOSITE neighbour's rise (the slope carried on past
      the edge), so water can leave the grid at the edge; that share is lost to the grid.
    - D8 (METHOD 0): all to the steepest lower neighbour; a cell with any neighbour outside the grid or without data
      passes nothing (SAGA's neighbour search gives up at an edge).
    Measured 2026-10-28 against saga_cmd 9.12.4 (gate 03). return_exit=True also gives, per cell, the water that ends
    there or leaves the grid from there (its sum = the total area: nothing is created or lost unseen).
    Vectorised: a cell passes its water on as soon as all its donors are done (a topological order)."""
    rows, cols = z.shape
    n = rows * cols
    valid = np.isfinite(z).ravel()
    zp = np.pad(z, 1, constant_values=np.nan)
    flat_idx = np.pad(np.arange(n).reshape(rows, cols), 1, constant_values=-1)
    steps = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]  # N, NE, E ... NW (as SAGA's 0..7)
    nbs = [zp[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols] for dr, dc in steps]
    dist = [math.hypot(dr * dy, dc * dx) for dr, dc in steps]
    inside = [np.isfinite(nb) for nb in nbs]  # a neighbour in the grid and with data
    w = np.zeros((8, n))
    to = np.full((8, n), -1, dtype=np.int64)
    for k, (dr, dc) in enumerate(steps):
        to[k] = np.where(inside[k], flat_idx[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols], -1).ravel()
        with np.errstate(invalid="ignore"):
            if mfd:
                opp = nbs[(k + 4) % 8]
                drop = np.where(inside[k], z - nbs[k], np.where(np.isfinite(opp), opp - z, 0.0))
                w[k] = np.where(np.isfinite(drop) & (drop > 0), (drop / dist[k]) ** convergence, 0.0).ravel()
            else:
                drop = np.where(inside[k], (z - nbs[k]) / dist[k], 0.0)
                w[k] = np.where(np.isfinite(drop) & (drop > 0), drop, 0.0).ravel()
    if not mfd:
        edge = ~np.logical_and.reduce(inside).ravel()
        best = w.argmax(axis=0)  # first of equal maxima, as SAGA's search (N first)
        has = (w.max(axis=0) > 0) & ~edge
        w = np.zeros_like(w)
        w[best[has], np.flatnonzero(has)] = 1.0
    total = w.sum(axis=0)
    w = np.divide(w, total, out=np.zeros_like(w), where=total > 0)
    acc = np.where(valid, dx * dy, 0.0)
    donors = np.zeros(n, dtype=np.int64)
    for k in range(8):
        np.add.at(donors, to[k][(w[k] > 0) & (to[k] >= 0)], 1)
    ready = np.flatnonzero(valid & (donors == 0))
    while ready.size:
        touched = []
        for k in range(8):
            give = ready[(w[k][ready] > 0) & (to[k][ready] >= 0)]
            if give.size:
                np.add.at(acc, to[k][give], acc[give] * w[k][give])
                np.add.at(donors, to[k][give], -1)
                touched.append(to[k][give])
        if not touched:
            break
        cand = np.unique(np.concatenate(touched))
        ready = cand[donors[cand] == 0]
    out = acc.reshape(rows, cols)
    out[~valid.reshape(rows, cols)] = np.nan
    if not return_exit:
        return out
    kept = sum(np.where(to[k] >= 0, w[k], 0.0) for k in range(8))  # share that stays in the grid
    exit_ = np.where(valid, acc * (1.0 - kept), 0.0).reshape(rows, cols)
    return out, exit_


def arrow_renderer():
    """One standard element: a Simple Marker of shape 'Arrow'. Size = field 'length' in map units and rotation = field
    'azimuth' (degrees clockwise from north), both as data-defined overrides on the symbol - the Size and Rotation rows
    of the Symbology page - so they can be changed there like on any layer."""
    arrow = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Arrow, 1.0)
    arrow.setColor(INK)
    arrow.setStrokeColor(INK)
    symbol = QgsMarkerSymbol([arrow])
    # the whole symbol in map units: with only the size in map units (0.2.0) the Symbology page showed "Millimeters",
    # because offset and stroke were still mm and QGIS then reports the symbol's unit as mixed (gui test 2026-10-27)
    symbol.setOutputUnit(Qgis.RenderUnit.MapUnits)
    symbol.setDataDefinedSize(QgsProperty.fromField("length"))
    symbol.setDataDefinedAngle(QgsProperty.fromField("azimuth"))
    return QgsSingleSymbolRenderer(symbol)


def lines_renderer():
    return QgsSingleSymbolRenderer(QgsLineSymbol([QgsSimpleLineSymbolLayer(INK, 0.3)]))


class _Style(QgsProcessingLayerPostProcessorInterface):
    """Gives a loaded result layer its style. QGIS calls it after the run, so a Python reference must be kept."""
    kept = []

    def __init__(self, make_renderer):
        super().__init__()
        self.make_renderer = make_renderer
        _Style.kept.append(self)

    def postProcessLayer(self, layer, context, feedback):
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            layer.setRenderer(self.make_renderer())
            layer.triggerRepaint()


def read(path):
    ds = gdal.Open(path)
    if ds is None:
        raise QgsProcessingException(f"GDAL cannot open {path}")
    band = ds.GetRasterBand(1)
    a = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    if nodata is not None:
        a[a == nodata] = np.nan
    return a, ds.GetGeoTransform()


def blocks(a, ny, nx):
    """(block rows, ny, block cols, nx) view of a, padded with NaN at the right / bottom."""
    a = np.pad(a, ((0, -a.shape[0] % ny), (0, -a.shape[1] % nx)), constant_values=np.nan)
    return a.reshape(a.shape[0] // ny, ny, a.shape[1] // nx, nx)


class FlowArrows(QgsProcessingAlgorithm):
    def createInstance(self):
        return FlowArrows()

    def name(self):
        return "dl_flow_arrows"

    def displayName(self):
        return "Flow arrows (fall line / D8)"

    def group(self):
        return "DL QGIS Code"

    def groupId(self):
        return "dl_qgis_code"

    def shortHelpString(self):
        return (
            "A terrain (DTM) in, a point layer out: one point per cell (or per block of cells) with the attributes "
            "that say where water would flow from there, drawn as arrows.\n\n"
            "<b>Arrows show</b>\n"
            "- <i>Fall line</i> (default): the direction of steepest descent. Smooth; it points into hollows, where "
            "water collects.\n"
            "- <i>Flow direction D8</i>: the one of the 8 neighbouring cells water passes to, after the hollows are "
            "filled (as in SAGA's flow accumulation). It turns in 45-degree steps; across a filled hollow it follows "
            "the overflow route. The field fill_m says how much the filling raised the cell.\n\n"
            "<b>Density</b>: one arrow every n cells; 1 = every cell, 5 = one arrow per 5 x 5 cells. With the fall "
            "line and n > 1, each arrow is the average of the slope in its block (Advanced: or the single cell).\n\n"
            "<b>Style</b>: one Simple Marker 'Arrow' - Rotation from the field azimuth, Size from the field length "
            "(map units). Change colour, size or rotation in the Symbology page as for any layer, e.g. Size from "
            "slope_deg with the assistant.\n\n"
            "<b>Catchment area</b> (on by default): catch_m2 = the total upslope area that drains through the cell "
            "under each point, in m2, the cell itself included - as SAGA GIS 'Flow Accumulation (Top-Down)' with its "
            "default settings (multiple flow direction, convergence 1.1), on the sink-filled terrain. "
            "Advanced: D8 instead. Large values mark the flow paths; style Size or Colour from it with the assistant "
            "(a log scale helps).\n\n"
            "Fields - only those that have values for the chosen method: azimuth (degrees clockwise from north, "
            "downhill), slope_deg, length (map units), method, cells (cells averaged), catch_m2 (if ticked); with D8 "
            "also d8_code (0 = N ... 7 = NW) and fill_m (how much the filling raised the cell).\n\n"
            "<b>Arrows as lines</b> (optional, skipped unless you choose a file or a temporary layer): every arrow as "
            "a simple straight line, centred on its point and drawn downhill (first vertex uphill, last downhill), "
            "for DXF or Blender.\n\n"
            "Method: QGIS's own native:aspect / native:slope (Horn) and native:fillsinkswangliu (Wang & Liu 2006, "
            "QGIS's port of the SAGA GIS tool); the catchment routing is computed here to match SAGA's tool; rasters "
            "read with GDAL. Thanks to the QGIS, SAGA GIS and GDAL communities.\n\n"
            f"Digital Landscapes - version {VERSION} - GPL-3.0-or-later - "
            "https://github.com/DLandscapes/dl-flow-arrows"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("INPUT", "Terrain (DTM)"))
        self.addParameter(QgsProcessingParameterEnum("METHOD", "Arrows show", options=METHODS, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "EVERY", "Density: one arrow every ... cells (1 = every cell)",
            type=Qgis.ProcessingNumberParameterType.Integer, defaultValue=1, minValue=1))
        self.addParameter(QgsProcessingParameterBoolean(
            "CATCHMENT", "Also sample the catchment area (m2, as SAGA's flow accumulation)", defaultValue=True))
        flat = QgsProcessingParameterNumber("FLAT_DEG", "Leave out arrows flatter than (deg)",
                                            type=Qgis.ProcessingNumberParameterType.Double, defaultValue=0.5,
                                            minValue=0.0, maxValue=89.0)
        flat.setMetadata({"widget_wrapper": {"decimals": 1}})
        fill = QgsProcessingParameterNumber("FILL_MIN_SLOPE",
                                            "Minimum slope when filling sinks (deg) - D8 and catchment",
                                            type=Qgis.ProcessingNumberParameterType.Double, defaultValue=0.1,
                                            minValue=0.0, maxValue=10.0)
        fill.setMetadata({"widget_wrapper": {"decimals": 2}})
        for p in (QgsProcessingParameterEnum("SAMPLE", "Fall line: arrow from", options=SAMPLES, defaultValue=0),
                  QgsProcessingParameterEnum("LENGTH", "Arrow length", options=LENGTHS, defaultValue=0),
                  flat, fill,
                  QgsProcessingParameterEnum("CATCH_ROUTING", "Catchment: how water is shared between neighbours",
                                             options=ROUTINGS, defaultValue=0)):
            p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterFeatureSink("OUTPUT", "Flow arrows",
                                                            type=Qgis.ProcessingSourceType.VectorPoint))
        lines = QgsProcessingParameterFeatureSink("OUTPUT_LINES", "Arrows as lines (for DXF / Blender)",
                                                  type=Qgis.ProcessingSourceType.VectorLine, optional=True,
                                                  createByDefault=False)
        # QGIS 3.44 shows outputs even when flagged Advanced (gui test 2026-10-27): it stays visible, "[Skip output]"
        lines.setFlags(lines.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(lines)
        for key, text in (("ARROWS", "Arrows written"), ("LEFT_OUT", "Places left out (flat or no data)"),
                          ("SPACING_M", "Distance between arrows (m)"),
                          ("CELLS_PER_ARROW", "Cells per arrow (along x)")):
            self.addOutput(QgsProcessingOutputNumber(key, text))

    def processAlgorithm(self, parameters, context, feedback):
        # Note: QGIS writes GDAL statistics as <file>.aux.xml next to a raster when it loads it from a path - before
        # this code runs, as for every QGIS raster tool (measured 2026-10-27). This script writes nothing there itself.
        layer = self.parameterAsRasterLayer(parameters, "INPUT", context)
        if layer is None:
            raise QgsProcessingException(self.invalidRasterError(parameters, "INPUT"))
        method = self.parameterAsEnum(parameters, "METHOD", context)
        nx = self.parameterAsInt(parameters, "EVERY", context)  # >= 1: Processing refuses smaller values (gate G0)
        sample = self.parameterAsEnum(parameters, "SAMPLE", context)
        by_slope = self.parameterAsEnum(parameters, "LENGTH", context) == 1
        flat = self.parameterAsDouble(parameters, "FLAT_DEG", context)
        fill_min = self.parameterAsDouble(parameters, "FILL_MIN_SLOPE", context)
        catchment = self.parameterAsBool(parameters, "CATCHMENT", context)
        mfd = self.parameterAsEnum(parameters, "CATCH_ROUTING", context) == 0
        steps = QgsProcessingMultiStepFeedback(5, feedback)

        def child(alg, params):
            return processing.run(alg, params, context=context, feedback=steps, is_child_algorithm=True)

        temp = QgsProcessing.TEMPORARY_OUTPUT
        slope_path = child("native:slope", {"INPUT": layer, "Z_FACTOR": 1, "OUTPUT": temp})["OUTPUT"]
        steps.setCurrentStep(1)
        if method == 0:
            aspect_path = child("native:aspect", {"INPUT": layer, "Z_FACTOR": 1, "OUTPUT": temp})["OUTPUT"]
        slope, gt = read(slope_path)
        if gt[2] != 0 or gt[4] != 0:
            raise QgsProcessingException("Rotated rasters are not supported.")
        if method == 1 or catchment:  # the sink-filled terrain: D8 directions and / or the catchment routing
            fill_params = {"INPUT": layer, "BAND": 1, "MIN_SLOPE": fill_min, "OUTPUT_FILLED_DEM": temp}
            if method == 1:
                fill_params["OUTPUT_FLOW_DIRECTIONS"] = temp
            fill = child("native:fillsinkswangliu", fill_params)
            filled, _ = read(fill["OUTPUT_FILLED_DEM"])
            if filled.shape != slope.shape:
                raise QgsProcessingException("The filled terrain is not on the input grid.")
        steps.setCurrentStep(2)
        if feedback.isCanceled():
            return {}

        dx, dy = gt[1], abs(gt[5])
        rows, cols = slope.shape
        ny = max(1, int(math.floor(nx * dx / dy + 0.5)))  # square blocks on the ground, also for non-square cells
        spacing_m = nx * dx
        n_brow, n_bcol = -(-rows // ny), -(-cols // nx)
        if n_brow * n_bcol > MANY:
            feedback.pushWarning(f"Up to {n_brow * n_bcol:,} arrows - drawing them may be slow. A density of "
                                 f"{math.ceil(math.sqrt(n_brow * n_bcol / MANY)) * nx} or more keeps it under "
                                 f"{MANY:,}.")

        # block edges (cells), the middle cell of every block (where the arrow stands) and its centre on the map
        r0, c0 = np.arange(n_brow) * ny, np.arange(n_bcol) * nx
        r1, c1 = np.minimum(rows, r0 + ny), np.minimum(cols, c0 + nx)
        rc, cc = np.meshgrid(r0 + (r1 - r0 - 1) // 2, c0 + (c1 - c0 - 1) // 2, indexing="ij")
        x, y = gt[0] + (cc + 0.5) * dx, gt[3] - (rc + 0.5) * dy
        cells = np.ones(rc.shape, int)
        d8 = fill_m = None
        if method == 0:
            aspect, _ = read(aspect_path)
            tan = np.tan(np.radians(slope))
            east = np.where(slope == 0, 0.0, np.sin(np.radians(aspect)) * tan)  # downhill vector, length tan(slope)
            north = np.where(slope == 0, 0.0, np.cos(np.radians(aspect)) * tan)
            if sample == 0 and nx * ny > 1:
                be, bn = blocks(east, ny, nx), blocks(north, ny, nx)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks (no data)
                    e, n = np.nanmean(be, axis=(1, 3)), np.nanmean(bn, axis=(1, 3))
                cells = np.isfinite(be).sum(axis=(1, 3))
                x, y = np.meshgrid(gt[0] + (c0 + c1) / 2 * dx, gt[3] - (r0 + r1) / 2 * dy)  # the block's centre
                label = "fall line (block mean)"
            else:
                e, n = east[rc, cc], north[rc, cc]
                label = "fall line" if nx * ny == 1 else "fall line (cell)"
            s = np.degrees(np.arctan(np.hypot(e, n)))
            az = np.degrees(np.arctan2(e, n)) % 360.0
            ok = np.isfinite(e) & np.isfinite(n) & ((e != 0) | (n != 0))
        else:
            code, _ = read(fill["OUTPUT_FLOW_DIRECTIONS"])
            z, _ = read(layer.source().split("|")[0])
            d8 = code[rc, cc]
            s = slope[rc, cc]
            az = d8 * 45.0
            fill_m = filled[rc, cc] - z[rc, cc]
            label = "D8"
            ok = np.isfinite(d8) & np.isfinite(s) & (s > 0) & np.isfinite(fill_m)
        with np.errstate(invalid="ignore"):  # NaN comparisons: silent in numpy 2.4, not in every older numpy
            keep = ok & (s >= flat)
        steps.setCurrentStep(3)
        catch = None
        if catchment:
            # sampled at the cell under the point (for a block: its middle cell, where the arrow stands)
            catch = catchment_area(filled, dx, dy, mfd=mfd)[rc, cc]
            keep &= np.isfinite(catch)
        left_out = int(n_brow * n_bcol - keep.sum())
        steps.setCurrentStep(4)

        full = LENGTH_SHARE * min(nx * dx, ny * dy)
        top = float(np.percentile(s[keep], 95)) if keep.any() else 0.0
        if by_slope and top > 0:
            length = full * np.clip(s / top, MIN_LENGTH_SHARE, 1.0)
        else:
            length = np.full(s.shape, full)
        fields = arrow_fields(d8=d8 is not None, catchment=catch is not None)
        sink, dest = self.parameterAsSink(parameters, "OUTPUT", context, fields, Qgis.WkbType.Point, layer.crs())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, "OUTPUT"))
        lines, lines_dest = self.parameterAsSink(parameters, "OUTPUT_LINES", context, fields, Qgis.WkbType.LineString,
                                                 layer.crs())
        idx = np.argwhere(keep)
        for done, (i, j) in enumerate(idx):
            if done % 5000 == 0:
                if feedback.isCanceled():
                    break
                steps.setProgress(100.0 * done / len(idx))
            a_, L = float(az[i, j]), float(length[i, j])
            px, py = float(x[i, j]), float(y[i, j])
            f = QgsFeature(fields)
            values = [round(a_, 6), round(float(s[i, j]), 6), L, label, int(cells[i, j])]
            if catch is not None:
                values.append(round(float(catch[i, j]), 4))
            if d8 is not None:
                values += [int(d8[i, j]), round(float(fill_m[i, j]), 6)]
            f.setAttributes(values)
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(px, py)))
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            if lines is not None:
                hx, hy = L / 2 * math.sin(math.radians(a_)), L / 2 * math.cos(math.radians(a_))
                f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(px - hx, py - hy), QgsPointXY(px + hx, py + hy)]))
                lines.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        if context.willLoadLayerOnCompletion(dest):
            context.layerToLoadOnCompletionDetails(dest).setPostProcessor(_Style(arrow_renderer))
        if lines is not None and context.willLoadLayerOnCompletion(lines_dest):
            context.layerToLoadOnCompletionDetails(lines_dest).setPostProcessor(_Style(lines_renderer))
        feedback.pushInfo(f"DL Flow Arrows {VERSION} (Digital Landscapes) - QGIS {Qgis.version()}")
        feedback.pushInfo(f"{label}: {len(idx)} arrows, one every {nx} cell(s) = {spacing_m:g} m; "
                          f"{left_out} places left out (flatter than {flat:g} deg, or no data).")
        if catch is not None:
            feedback.pushInfo(f"catchment area (catch_m2): {ROUTINGS[0 if mfd else 1]}, on the terrain filled with a "
                              f"minimum slope of {fill_min:g} deg; largest sampled value "
                              f"{np.nanmax(catch[keep]):,.0f} m2." if keep.any() else "catchment area: no arrows.")
        result = {"OUTPUT": dest, "ARROWS": len(idx), "LEFT_OUT": left_out, "SPACING_M": spacing_m,
                  "CELLS_PER_ARROW": nx}
        if lines is not None:
            result["OUTPUT_LINES"] = lines_dest
        return result
