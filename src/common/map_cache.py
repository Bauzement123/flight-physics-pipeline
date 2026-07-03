"""
Map + Airport Builder Geographical Caching Module.
Pre-loads European topological geometries and airport coordinate registries
into read-only, in-memory structures to prevent multi-process I/O contention.
"""

import logging
import json
import matplotlib
matplotlib.use("Agg")

import pandas as pd
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.io.shapereader as shapereader

from src.common.config import AIRPORTS_CACHE_PATH

logger = logging.getLogger(__name__)

class EuropeanMapCache:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(EuropeanMapCache, cls).__new__(cls, *args, **kwargs)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.airports_df = pd.DataFrame(columns=["icao", "lat", "lon"])
        self.land_feature = None
        self.ocean_feature = None
        self.coastline_feature = None
        self.borders_feature = None
        self._initialized = False

    def initialize(self, resolution: str = "10m"):
        """
        Loads shapefiles and airport coordinates cache.
        Safe to call multiple times (performs initial load once).
        """
        if self._initialized:
            return self

        logger.info("Initializing EuropeanMapCache (pre-loading shapefiles and airports)...")
        
        # 1. Load Airport Coordinate Cache
        try:
            if AIRPORTS_CACHE_PATH.exists():
                with open(AIRPORTS_CACHE_PATH, "r", encoding="utf-8") as f:
                    coords = json.load(f)
                records = []
                for icao, pt in coords.items():
                    records.append({
                        "icao": icao,
                        "lat": pt["lat"],
                        "lon": pt["lon"]
                    })
                self.airports_df = pd.DataFrame(records)
                logger.info(f"Loaded {len(self.airports_df)} airports from {AIRPORTS_CACHE_PATH.name}.")
            else:
                logger.warning(f"Airport cache not found at {AIRPORTS_CACHE_PATH}. Background airport layer will be empty.")
        except Exception as e:
            logger.error(f"Failed to load airport coordinate cache: {e}", exc_info=True)

        # 2. Pre-load Topological Geometries
        try:
            logger.info("Loading NaturalEarth physical and cultural shapefiles...")
            
            def get_features(category: str, name: str) -> list:
                shp_path = shapereader.natural_earth(resolution=resolution, category=category, name=name)
                reader = shapereader.Reader(shp_path)
                return list(reader.geometries())

            land_geoms = get_features("physical", "land")
            ocean_geoms = get_features("physical", "ocean")
            coast_geoms = get_features("physical", "coastline")
            border_geoms = get_features("cultural", "admin_0_countries")

            # Create ShapelyFeature instances
            self.land_feature = cfeature.ShapelyFeature(
                land_geoms, ccrs.PlateCarree(), facecolor="#f5f5f3", edgecolor="none"
            )
            self.ocean_feature = cfeature.ShapelyFeature(
                ocean_geoms, ccrs.PlateCarree(), facecolor="#deebf7", edgecolor="none"
            )
            self.coastline_feature = cfeature.ShapelyFeature(
                coast_geoms, ccrs.PlateCarree(), facecolor="none", edgecolor="#525252", linewidth=0.6
            )
            self.borders_feature = cfeature.ShapelyFeature(
                border_geoms, ccrs.PlateCarree(), facecolor="none", edgecolor="#969696", linewidth=0.5
            )
            logger.info("Topological geometries loaded successfully.")

        except Exception as e:
            logger.error(f"Failed to load NaturalEarth shapefiles: {e}", exc_info=True)
            self.land_feature = cfeature.LAND
            self.ocean_feature = cfeature.OCEAN
            self.coastline_feature = cfeature.COASTLINE
            self.borders_feature = cfeature.BORDERS

        self._initialized = True
        return self

    def add_features_to_axes(self, ax):
        """
        Adds in-memory land, ocean, borders, and coastlines to a cartopy GeoAxes.
        """
        if not self._initialized:
            self.initialize()
        
        # Add features in proper drawing order
        ax.add_feature(self.ocean_feature, zorder=0)
        ax.add_feature(self.land_feature, zorder=0)
        ax.add_feature(self.borders_feature, zorder=1)
        ax.add_feature(self.coastline_feature, zorder=1)
