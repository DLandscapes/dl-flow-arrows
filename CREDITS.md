# DL Flow Arrows - credits

DL Flow Arrows is made by Digital Landscapes (https://digital-landscapes.com). It is released under the GNU General
Public License, version 3 or (at your option) any later version; see `LICENSE`. It stands on open-source work by
others. Thank you.

Copyright 2026 Marc Ihle (Digital Landscapes).

## Built on
- **QGIS** and its Processing framework (https://qgis.org), in particular its algorithms `native:slope`,
  `native:aspect` (Horn's method) and `native:fillsinkswangliu`, which QGIS's help describes as "a port of the SAGA
  'Fill Sinks (Wang & Liu)' tool".
- **GDAL** (https://gdal.org) for reading rasters, and **numpy** (https://numpy.org), both as shipped with QGIS.

## Matched to, and tested against
- **SAGA GIS** (https://saga-gis.sourceforge.io), tool *Flow Accumulation (Top-Down)* (`ta_hydrology 0`). `saga_cmd`
  names its authors as "O.Conrad (c) 2001-2016, Portions by T.Grabs (c) 2010". The catchment routing in this script was
  written to give the same results, including the behaviour at the grid edge, after reading that tool's source
  (`Flow_Parallel.cpp`). It is a new implementation; no SAGA code is copied. SAGA 9.12.4, the copy that ships with
  QGIS 3.44.12, served as the reference in the tests.

## Test data (not included)
Two 1 m survey terrains were used for testing and are not part of this repository. All files in this repository are
made with made-up terrains.

## How it was made
Built by Digital Landscapes with the help of an AI coding assistant (Claude by Anthropic, in Claude Code). Digital
Landscapes set the goals, took the design decisions, and reviewed and tested every version; the assistant wrote much
of the code. Every version passed a test gate in QGIS 3.44.12, and the catchment was checked cell by cell against SAGA
GIS.
