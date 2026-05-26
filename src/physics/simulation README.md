Loop 3b: Physics Simulation Module

This module represents the final execution engine of the Flight Physics Pipeline. It mathematically bridges the geodetic flight paths with the atmospheric cache to compute real-world aircraft performance and contrail radiative forcing.

Module Structure

src/physics/
├── simulation.py          # Runs the PSFlight and Cocip models sequentially
└── README.md              # This file


Workflow

The simulation engine is designed as a functional executor. It does not download data.

Hydration: Uses the traffic_adapter.py utility to load flat .parquet data and structure it into dictionaries of pycontrails.Flight objects.

Aircraft Performance (PSFlight): Computes True Airspeed (TAS), fuel mass flow rates, and dynamic engine emissions indices (NOx, soot) based on the aircraft typecode and the ambient 3D ERA5 weather grids.

Contrail Modeling (Cocip): Feeds the aerodynamically-enriched flights into the Contrail Cirrus Prediction model to evaluate the Schmidt-Appleman criterion, contrail persistence, and total Radiative Forcing (RF).

Serialization: Re-flattens the enriched trajectory objects and exports a single, high-fidelity _simulated.parquet file.

Usage:

# 1. Process a single clean file:
python -m src.physics.simulation `
    --input-file "data/trajectories/ranks_1_strat_fixed_val_2.0_seed_42_format_oneway_ee7a02/clean/LEPA-LEBL_ab1081_clean_si.parquet" `
    --out-dir "data/results/test_scenario" `
    --weather-cache "data/weather" `
    --start-date "2025-01-01T11:00:00" `
    --end-date "2025-01-01T13:00:00"

# 2. Process an entire directory of clean files:
python -m src.physics.simulation `
    --input-file "data/trajectories/ranks_1_strat_fixed_val_2.0_seed_42_format_oneway_ee7a02/clean" `
    --out-dir "data/results/test_scenario" `
    --weather-cache "data/weather" `
    --start-date "2025-01-01T11:00:00" `
    --end-date "2025-01-01T13:00:00"

**Parameters**:
- `--input-file`: Path to cleaned SI trajectory Parquet file OR directory containing multiple cleaned Parquet files (scans for `*_clean_si.parquet`).
- `--out-dir`: Output directory for simulated results.
- `--weather-cache`: Directory containing ERA5 NetCDF cache files.
- `--start-date` & `--end-date`: Temporal window for weather query lookup (supports YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).

Prerequisites

Your data/weather/ directory must be populated (Loop 3a) with ERA5 NetCDF weather data covering the temporal bounds of the trajectory.