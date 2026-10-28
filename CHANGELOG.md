# Changelog

## 1.0.0 - 2026-10-28

First public version. This is the development version 0.3.2 after a full code review, with the licence (GPL-3.0-or-later)
and citation added. Its output is identical to 0.3.2: feature by feature on nine test cases, about 160,000 features.

## Development versions (not published)

- **0.3.2:** code review. Removed one unused variable, one repeated style class and three duplicated computations;
  wrapped long lines. The output is identical to 0.3.1.
- **0.3.1:** the catchment routing follows SAGA's edge rules. With MFD, water can leave the grid over the edge; with
  D8, edge cells pass nothing on. Since then the routing matches SAGA on 100 % of the cells on SAGA's own filled
  terrain: D8 exactly, MFD to within 0.0001 %.
- **0.3.0:** new field `catch_m2`, the catchment area. A layer now carries only the fields of its method; before,
  `d8_code` and `fill_m` stayed empty for the fall line.
- **0.2.1:** the whole arrow symbol is in map units, so the Symbology page shows "Map Units".
- **0.2.0:** one standard Simple Marker "Arrow" instead of a nested vector-field symbol. Density is set in cells,
  default every cell, instead of an automatic spacing. Arrows as lines became simple lines.
- **0.1.x:** the first versions: fall line and D8 arrows from QGIS's own aspect and sink-filling tools.
