import re
import datetime
from datetime import datetime
from pathlib import Path
from shapely.geometry import shape
from util.io import IOManager
from util.handler import FileHandler


_TIMESTAMP_PATTERNS = [
    re.compile(r"MRMS_MergedReflectivityQC_3D_(\d{8})-(\d{6})"),
    re.compile(r"(\d{8})-(\d{6})_renamed"),
    re.compile(r"(\d{8}-\d{6})"),
    re.compile(r".*(\d{8})-(\d{6}).*"),
    re.compile(r"s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)"),
]

_DETECTION_IO = IOManager("[CellDetection]")


def _normalize_geojson_longitudes(coordinates):
    """Return GeoJSON coordinates with longitude expressed in [-180, 180)."""
    if (
        isinstance(coordinates, (list, tuple))
        and len(coordinates) >= 2
        and isinstance(coordinates[0], (int, float))
        and isinstance(coordinates[1], (int, float))
    ):
        return [((coordinates[0] + 180) % 360) - 180, *coordinates[1:]]
    if isinstance(coordinates, (list, tuple)):
        return [_normalize_geojson_longitudes(value) for value in coordinates]
    return coordinates

class DetectionDataHandler:
    def __init__(self, radar_path, ps_path, preciptype_path, io_manager, lat_min, lat_max, lon_min, lon_max, 
                 radar_obj=None, ps_obj=None, preciptype_obj=None):
        """
        Initialize the RadarDataHandler.

        Parameters:
            radar_path (str): Path to radar dataset
            ps_path (str): Path to ProbSevere dataset
            preciptype_path (str): Path to precipitation type dataset
            io_manager (IOManager): IO manager instance
            lat_min, lat_max (float): Latitude bounds for the subset
            lon_min, lon_max (float): Longitude bounds for the subset
            radar_obj (xarray.Dataset, optional): Pre-loaded radar dataset
            ps_obj (dict, optional): Pre-loaded ProbSevere data
            preciptype_obj (xarray.Dataset, optional): Pre-loaded PrecipType dataset
        """
        self.radar_path = radar_path
        self.ps_path = ps_path
        self.preciptype_path = preciptype_path
        self.lat_grid = (lat_min, lat_max)
        self.lon_grid = (lon_min, lon_max)
        self.dataset = None
        self.io_manager = io_manager
        self.file_handler = FileHandler(io_manager)
        
        # Cache
        self.radar_obj = radar_obj
        self.ps_obj = ps_obj
        self.preciptype_obj = preciptype_obj

    def load_radar_full(self):
        """
        Load the full MRMS radar dataset from file without subsetting.
        Uses the centralized FileHandler.load_dataset method.
        """
        return self.file_handler.load_dataset(self.radar_path)

    def subset_radar(self, ds):
        """
        Subset the given radar dataset using the stored lat/lon grid limits.
        """
        return self.file_handler.subset_dataset(
            ds, 
            lat_limits=self.lat_grid,
            lon_limits=self.lon_grid
        )

    def load_subset(self):
        """
        Load the MRMS radar dataset from file and return a lat/lon subset as xarray.Dataset.
        Uses the centralized FileHandler.load_dataset method.
        """
        if self.radar_obj is not None:
             return self.radar_obj

        return self.file_handler.load_dataset(
            self.radar_path,
            lat_limits=self.lat_grid,
            lon_limits=self.lon_grid
        )
    
    def load_preciptype(self):
        """
        Load the precipitation type dataset from file and return a lat/lon subset as xarray.Dataset.
        Uses the centralized FileHandler.load_dataset method.
        """
        if self.preciptype_obj is not None:
            return self.preciptype_obj
            
        return self.file_handler.load_dataset(
            self.preciptype_path,
            lat_limits=self.lat_grid,
            lon_limits=self.lon_grid
        )
    
    def load_probsevere(self):
        """
        Load ProbSevere GeoJSON from specified path,
        returning only polygons with at least one vertex in the lat/lon range.
        Uses the centralized FileHandler.load_dataset method for loading.
        """
        # Load the JSON data using FileHandler
        if self.ps_obj is not None:
            data = self.ps_obj
        else:
            data = self.file_handler.load_dataset(self.ps_path)
        
        if data is None:
            return []
        
        # Filter features based on lat/lon bounds
        lat_min, lat_max = self.lat_grid
        lon_min, lon_max = self.lon_grid

        # Normalize to -180 to 180 range
        lat_min = (lat_min + 180) % 360 - 180
        lat_max = (lat_max + 180) % 360 - 180
        lon_min = (lon_min + 180) % 360 - 180
        lon_max = (lon_max + 180) % 360 - 180

        filtered_features = []

        for feature in data.get('features', []):
            geometry = feature.get('geometry')
            if not geometry:
                continue

            normalized_geometry = dict(geometry)
            if 'coordinates' in normalized_geometry:
                normalized_geometry['coordinates'] = _normalize_geojson_longitudes(
                    normalized_geometry['coordinates']
                )
            geom = shape(normalized_geometry)
            if geom.is_empty:
                continue

            min_lon, min_lat, max_lon, max_lat = geom.bounds

            if (
                min_lon <= lon_max
                and max_lon >= lon_min
                and min_lat <= lat_max
                and max_lat >= lat_min
            ):
                normalized_feature = dict(feature)
                normalized_feature['geometry'] = normalized_geometry
                filtered_features.append(normalized_feature)

        filtered_data = dict(data)
        filtered_data['features'] = filtered_features
        return filtered_data
    
    @staticmethod
    def find_timestamp(filepath):
        """
        Finds timestamps in a file based on predetermined patterns
        """
        filename = Path(filepath).name

        for pattern in _TIMESTAMP_PATTERNS:
            match = pattern.search(filename)
            if match:
                groups = match.groups()

                if len(groups) == 2:
                    date_str, time_str = groups
                elif len(groups) == 1 and len(groups[0]) >= 15:  # 'YYYYMMDD-HHMMSS' min length
                    combined = groups[0]
                    date_str, time_str = combined[:8], combined[9:15]
                else:
                    continue

                try:
                    formatted_time = (f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T"
                                    f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}")
                    _DETECTION_IO.write_info(f"Extracted timestamp: {formatted_time}")
                    return formatted_time
                except (IndexError, ValueError) as e:
                    _DETECTION_IO.write_warning(f"Error formatting timestamp: {e}")
                    continue

        fallback = datetime.utcnow().isoformat()
        _DETECTION_IO.write_warning(f"Using fallback timestamp: {fallback}")
        return fallback

    @staticmethod
    def latest_file(directory, pattern="*"):
        """
        Get the latest file in a directory matching a pattern.
        """
        try:
            files = sorted(Path(directory).glob(pattern))
            if not files:
                return None
            return files[-1]
        except Exception:
            return None
