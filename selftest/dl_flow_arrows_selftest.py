# SPDX-FileCopyrightText: 2026 Marc Ihle (Digital Landscapes)
# SPDX-License-Identifier: GPL-3.0-or-later
"""DL Flow Arrows - self-test.

Run it in QGIS: Plugins > Python Console > Show Editor (the notepad icon) > Open Script... > this file > Run Script.
It uses the tool as INSTALLED in the Processing Toolbox ("script:dl_flow_arrows"), builds small made-up terrains in a
temporary folder, runs 12 checks with known answers and prints a report: QGIS version, operating system, the installed
tool's version and PASS / FAIL per check. Your project and your own files are not changed.
If a check fails, please send the report text (it names your system and versions, nothing personal).
"""
import math
import os
import platform
import re
import tempfile

import numpy as np
import processing
from osgeo import gdal, osr
from qgis.core import Qgis, QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsVectorLayer

ALG = "script:dl_flow_arrows"


def selftest(report_path=None):
    work = tempfile.mkdtemp(prefix="dl_flow_arrows_selftest_")
    lines, results = [], []

    def check(label, ok, detail=""):
        results.append(bool(ok))
        lines.append(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}")

    def terrain(name, z):
        path = os.path.join(work, name + ".tif")
        ds = gdal.GetDriverByName("GTiff").Create(path, z.shape[1], z.shape[0], 1, gdal.GDT_Float32)
        ds.SetGeoTransform((500000.0, 1.0, 0.0, 7700000.0, 0.0, -1.0))  # 1 m cells, ETRS89 / UTM 33N
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(25833)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(-9999)
        ds.GetRasterBand(1).WriteArray(z.astype("float32"))
        ds = None
        return path

    counter = iter(range(1, 10_000))

    def run(dtm, **params):
        out = os.path.join(work, f"run {next(counter):03d}.gpkg")
        p = {"INPUT": dtm, "OUTPUT": out}
        if params.pop("lines", False):
            p["OUTPUT_LINES"] = out.replace(".gpkg", " lines.gpkg")
        p.update(params)
        res = processing.run(ALG, p, context=QgsProcessingContext(), feedback=QgsProcessingFeedback())
        lyr = QgsVectorLayer(res["OUTPUT"], "points", "ogr")
        names = [n for n in lyr.fields().names() if n != "fid"]
        feats = [({n: f[n] for n in names}, f.geometry()) for f in lyr.getFeatures()]
        return res, names, feats

    def angdiff(a, b):
        return abs((a - b + 180.0) % 360.0 - 180.0)

    def isnull(v):
        return v is None or str(v) == "NULL"

    lines.append(f"DL Flow Arrows self-test - QGIS {Qgis.version()} - {platform.platform()} - Python "
                 f"{platform.python_version()} - numpy {np.__version__} - GDAL {gdal.__version__}")
    alg = QgsApplication.processingRegistry().algorithmById(ALG)
    version = None
    if alg is not None:
        m = re.search(r"version ([0-9.]+)", alg.shortHelpString())
        version = m.group(1) if m else "?"
    check("1 the tool is installed in the Processing Toolbox", alg is not None, f"version {version}")
    if alg is None:
        lines.append("Install it first: Processing Toolbox > Python icon > Add Script to Toolbox... > dl_flow_arrows.py")
        return finish(lines, results, work, report_path)

    # 2 only one copy: QGIS uses the first of several files that define the same tool, silently
    copies = []
    try:
        from processing.script.ScriptUtils import scriptsFolders
        for folder in scriptsFolders():
            if os.path.isdir(folder):
                for fn in os.listdir(folder):
                    if fn.lower().endswith(".py"):
                        with open(os.path.join(folder, fn), encoding="utf-8", errors="ignore") as fh:
                            if re.search(r"return\s+[\"']dl_flow_arrows[\"']", fh.read()):
                                copies.append(os.path.join(folder, fn))
    except Exception as ex:  # noqa: BLE001
        copies = [f"(scripts folder not readable: {ex})"]
    check("2 exactly one copy of the tool in the scripts folders", len(copies) == 1,
          "" if len(copies) == 1 else f"found {len(copies)}: {copies} - QGIS uses only the first by name; close QGIS, "
                                      "delete the old copies, add the new file again")

    cols, rows = 60, 40
    c, r = np.meshgrid(np.arange(cols) + 0.5 - cols / 2, rows / 2 - (np.arange(rows) + 0.5))
    t10 = math.tan(math.radians(10.0))
    # 3 + 4: planes, 10 deg steep, downhill toward azimuth A
    worst_fall, worst_d8 = 0.0, 0.0
    for A in (0, 45, 90, 135, 180, 225, 270, 315, 30, 200):
        dtm = terrain(f"plane {A}", -t10 * (c * math.sin(math.radians(A)) + r * math.cos(math.radians(A))))
        _, _, fs = run(dtm, EVERY=5, CATCHMENT=False)
        worst_fall = max([worst_fall] + [angdiff(a["azimuth"], A) for a, _ in fs])
        _, _, fs = run(dtm, EVERY=5, METHOD=1, CATCHMENT=False)
        step = (round(A / 45.0) * 45) % 360
        worst_d8 = max([worst_d8] + [angdiff(a["azimuth"], step) for a, _ in fs])
    check("3 fall line on 10 planes points downhill", worst_fall <= 0.01, f"worst {worst_fall:.5f} deg")
    check("4 D8 on 10 planes = the nearest 45-deg step", worst_d8 <= 1e-9, f"worst {worst_d8:.5f} deg")

    east = terrain("plane east", -t10 * c)
    res, names, fs = run(east)
    check("5 default: one arrow per cell, no blank fields",
          len(fs) == cols * rows and names == ["azimuth", "slope_deg", "length", "method", "cells", "catch_m2"]
          and not any(isnull(v) for a, _ in fs for v in a.values()), f"{len(fs)} arrows, fields {names}")

    rr = np.hypot(*np.meshgrid(np.arange(81) + 0.5 - 40.5, 40.5 - (np.arange(81) + 0.5)))
    cx, cy = 500000.0 + 40.5, 7700000.0 - 40.5
    for label, z, inward in (("6 cone: arrows point away from the top", 60 - 0.5 * rr, False),
                             ("7 bowl: arrows point into the hollow", 0.5 * rr, True)):
        _, _, fs = run(terrain(label[:6].strip(), z), EVERY=3, SAMPLE=1, CATCHMENT=False)
        worst = 0.0
        for a, g in fs:
            p = g.asPoint()
            if math.hypot(p.x() - cx, p.y() - cy) >= 5:
                out_az = math.degrees(math.atan2(p.x() - cx, p.y() - cy)) % 360.0
                worst = max(worst, angdiff(a["azimuth"], (out_az + 180.0) % 360.0 if inward else out_az))
        check(label, worst <= 1.0, f"worst {worst:.3f} deg")

    # 8 + 9: catchment on the east plane (water runs east, row by row)
    def col_row(g):
        p = g.asPoint()
        return int(p.x() - 500000.0), int(7700000.0 - p.y())

    _, _, fs = run(east, CATCH_ROUTING=1)
    bad = 0
    for a, g in fs:
        cc, rw = col_row(g)
        want = 1.0 if (cc == 0 or rw in (0, rows - 1)) else float(cc)  # SAGA's D8: edge cells pass nothing on
        bad += abs(a["catch_m2"] - want) > 1e-3
    check("8 catchment, D8 routing: known values on a plane", bad == 0, f"{bad} of {len(fs)} cells differ")
    _, _, fs = run(east)
    bad = n = 0
    for a, g in fs:
        cc, rw = col_row(g)
        if min(rw, rows - 1 - rw) > cc:
            n += 1
            bad += abs(a["catch_m2"] - (cc + 1)) > 1e-3
    check("9 catchment, MFD routing: known values on a plane", n > 0 and bad == 0, f"{bad} of {n} cells differ")

    _, names, fs = run(east, METHOD=1)
    check("10 D8: two more fields, no blank fields", names[-2:] == ["d8_code", "fill_m"]
          and not any(isnull(v) for a, _ in fs for v in a.values()), f"fields {names}")

    res, _, fs = run(east, EVERY=5, lines=True)
    lyr = QgsVectorLayer(res["OUTPUT_LINES"], "lines", "ogr")
    bad = 0
    for (a, g), ln in zip(fs, lyr.getFeatures()):
        v = ln.geometry().asPolyline()
        mid = ((v[0].x() + v[1].x()) / 2, (v[0].y() + v[1].y()) / 2)
        bad += not (len(v) == 2 and math.hypot(mid[0] - g.asPoint().x(), mid[1] - g.asPoint().y()) < 1e-6
                    and abs(math.hypot(v[1].x() - v[0].x(), v[1].y() - v[0].y()) - a["length"]) < 1e-6)
    check("11 arrows as lines: one centred line per arrow, the right length", bad == 0 and lyr.featureCount() == len(fs),
          f"{lyr.featureCount()} lines, {bad} wrong")

    res, _, fs = run(terrain("flat", np.full((30, 30), 10.0)), EVERY=5)
    check("12 flat ground: no arrows, no error", len(fs) == 0 and res["LEFT_OUT"] == 36, f"{len(fs)} arrows")
    return finish(lines, results, work, report_path)


def finish(lines, results, work, report_path):
    summary = f"DL Flow Arrows self-test: {sum(results)} of {len(results)} checks passed"
    lines += [summary, f"(test files in {work} - safe to delete)"]
    text = "\n".join(lines)
    print(text)
    with open(report_path or os.path.join(work, "self-test report.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    try:
        from qgis.utils import iface
        if iface is not None:
            level = Qgis.MessageLevel.Success if all(results) else Qgis.MessageLevel.Critical
            iface.messageBar().pushMessage("DL Flow Arrows", summary + " - the full report is in the Python Console.",
                                           level, 0)
    except Exception:  # noqa: BLE001 - no QGIS window (headless test)
        pass
    return all(results), text


selftest(os.environ.get("DL_SELFTEST_REPORT"))  # runs when the file is run (QGIS console editor: Run Script)
