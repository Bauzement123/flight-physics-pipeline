Flight Physics Kinematic & Weather Pipeline V2

A high-performance, functionally-composed Python pipeline designed to transform raw ADS-B data into 6D kinematic states, enrich them with high-fidelity ERA5 atmospheric data, and run predictive aircraft performance and contrail modeling (PSFlight & Cocip).

Architecture & Loops

This pipeline strictly separates concerns to ensure network fragility (e.g., Trino timeouts or Copernicus API limits) does not crash mathematical or simulation operations.

Loop 1: Acquisition (src/acquisition/) - Slices master lists and downloads raw state vectors from the OpenSky Trino database.

Loop 2: Processing (src/processing/) - Filters noise, drops ground-state data, and applies Extended Kalman Filter (EKF) mathematical smoothing. Converts non-SI aviation units to strict SI metric units.

Loop 3a: Weather (src/weather/era5_manager.py) - Determines the spatial bounding box needed, connects to Copernicus, and lazily downloads/updates a shared NetCDF disk cache.

Loop 3b: Simulation (src/physics/simulation.py) - Maps the flight trajectories over the 3D weather grids, evaluates engine fuel flow/emissions, and calculates contrail radiative forcing.

Executing the Pipeline

The pipeline provides a master orchestrator (run_all.py) that handles dynamic parameter passing between the functionally independent CLI tools.

End-to-End Execution

python src/run_all.py `
    --start-date "2025-01-01" `
    --end-date "2025-01-02" `
    --typecode "B738" `
    --origin "EGLL" `
    --sample-size 10


Checkpointing (Fast-Track Simulation)

Downloading millions of waypoints from Trino takes time. If you have already executed Loop 1 and possess a _raw.parquet file, you can bypass the database connection and resume directly at Loop 2.

python src/run_all.py `
    --start-date "2025-01-01" `
    --end-date "2025-01-02" `
    --typecode "B738" `
    --origin "EGLL" `
    --start-from-raw "data/trajectories/ranks_1_strat_fixed_val_2.0_seed_42_format_oneway_ee7a02/raw/LEPA-LEBL_ab1081_raw.parquet"


Core Dependencies

traffic

pycontrails[ecmwf]

pyopensky

pandas & pyarrow

Active Copernicus CDS (CDSAPI) environment credentials.