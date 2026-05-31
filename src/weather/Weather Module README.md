Loop 3a: Weather Acquisition Module

This module is responsible for the independent, bulk acquisition of atmospheric reanalysis data required for contrail and performance modeling.

It operates as Loop 3a of the Flight Physics Pipeline. It is decoupled from flight data, allowing you to run bulk downloads across multiple machines or in parallel batches, populating a centralized cache for the physics simulations (Loop 3b) to consume.

Module Structure

src/weather/
├── era5_manager.py        # Standalone weather fetcher (CLI)
└── README.md              # This file


Workflow

The era5_manager.py script uses the pycontrails interface to the Copernicus Climate Data Store (CDS). It downloads high-fidelity ERA5 atmospheric reanalysis data (temperature, relative humidity, and wind vectors) for a specified spatiotemporal bounding box.

Usage (PowerShell):

python src/weather/era5_manager.py `
    --start "2025-01-01" `
    --end "2025-01-02"

python src/weather/era5_manager.py `
    --start "2025-01-01T00:00:00" `
    --end "2025-01-01T03:00:00" `
    --debug    

--bbox: Format as min_lon, min_lat, max_lon, max_lat.

--start / --end: Format as YYYY-MM-DD.

The downloaded NetCDF files are automatically saved to data/weather/.

Prerequisites

Dependencies: Requires pycontrails installed with ECMWF extras:
pip install "pycontrails[ecmwf]"

Authentication: You must have your Copernicus Climate Data Store (CDS) credentials configured in your environment variables (CDSAPI_URL and CDSAPI_KEY).

Design Paradigm

This module is designed for standalone capability. It does not ingest flight trajectories; instead, it proactively populates the data/weather/ directory. When Loop 3b (the physics simulation) runs, it will detect these cached files and use them for Cocip modeling, significantly reducing latency and redundant API calls.