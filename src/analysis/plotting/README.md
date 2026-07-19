# Analysis Plotting Module

The `src/analysis/plotting` module provides geographic map rendering tools and verification scripts for visualizing cached European basemaps (`EuropeanMapCache`) and overlaid airport coordinate registries.

---

## 1. Module Structure

```text
src/analysis/plotting/
├── verify_map.py        # Verification CLI script for EuropeanMapCache and airport overlays
└── README.md            # This module documentation
```

---

## 2. Functions & Execution Entrypoints

### `verify_map.py`
Renders the 10m NaturalEarth European basemap cached by `EuropeanMapCache` (`src.common.map_cache`), overlays all cached airport coordinates (with major airport ICAOs labeled), draws gridlines, and saves output figures to `data/analysis/plots/verify_map.svg` and `verify_map.png`.

---

## 3. CLI Usage Guide

```bash
# Execute map cache verification script
python -m src.analysis.plotting.verify_map
```

```powershell
# Execute map cache verification script (PowerShell)
python -m src.analysis.plotting.verify_map
```

---

## 4. Output Artifacts

Outputs are saved to `data/analysis/plots/`:
* `data/analysis/plots/verify_map.svg` (vector format)
* `data/analysis/plots/verify_map.png` (high-res raster format, 150 DPI)
