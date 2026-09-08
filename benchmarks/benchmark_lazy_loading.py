#!/usr/bin/env python3
"""
Benchmark: Lazy Dataset Loading vs Eager Loading

This benchmark compares loading strategies for the storm cell integration pipeline:
1. GRIB Fast Loader (custom eccodes-based) - Fast but memory-intensive
2. NetCDF Eager Loading (xarray with .load()) - Baseline
3. NetCDF Lazy Loading (xarray with .isel().compute()) - Memory-optimized

Metrics:
- Peak memory usage
- Total execution time
- Memory usage over time
"""

import time
import tracemalloc
import numpy as np
import xarray as xr
from pathlib import Path
import tempfile
import gc
from typing import List, Dict, Any
import json

# Ensure src is in path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shapely.geometry import Polygon
import shapely.vectorized as sv
from util.grib_loader import load_grib_fast
try:
    import eccodes
    ECCODES_AVAILABLE = True
except ImportError:
    ECCODES_AVAILABLE = False
    print("Warning: eccodes not available, GRIB benchmarks will be skipped")


# =============================================================================
# Synthetic Data Generation
# =============================================================================

def create_synthetic_grib_file(
    filepath: Path,
    lat_size: int = 3500,
    lon_size: int = 7000
) -> Path:
    """Create a synthetic GRIB2 file using eccodes."""
    if not ECCODES_AVAILABLE:
        return None
    
    # Create sample data
    data = np.random.random((lat_size, lon_size)).astype(np.float64) * 50
    
    # Add storm cells
    for _ in range(20):
        cy = np.random.randint(500, lat_size - 500)
        cx = np.random.randint(500, lon_size - 500)
        radius = np.random.randint(20, 100)
        intensity = np.random.random() * 30 + 40
        
        y, x = np.ogrid[-radius:radius, -radius:radius]
        mask = x**2 + y**2 <= radius**2
        
        y_start = max(0, cy - radius)
        y_end = min(lat_size, cy + radius)
        x_start = max(0, cx - radius)
        x_end = min(lon_size, cx + radius)
        
        y_slice = slice(y_start - cy + radius, y_end - cy + radius)
        x_slice = slice(x_start - cx + radius, x_end - cx + radius)
        
        if mask[y_slice, x_slice].shape == (y_end - y_start, x_end - x_start):
            data[y_start:y_end, x_start:x_end][mask[y_slice, x_slice]] = intensity
    
    # Create GRIB message
    with open(filepath, 'wb') as f:
        # Create a new GRIB message
        gid = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
        
        # Set grid dimensions
        eccodes.codes_set_long(gid, 'Ni', lon_size)
        eccodes.codes_set_long(gid, 'Nj', lat_size)
        
        # Set grid extents (MRMS-like: 20-55°N, -130 to -60°W)
        eccodes.codes_set_double(gid, 'latitudeOfFirstGridPointInDegrees', 20.0)
        eccodes.codes_set_double(gid, 'longitudeOfFirstGridPointInDegrees', -130.0)
        eccodes.codes_set_double(gid, 'latitudeOfLastGridPointInDegrees', 55.0)
        eccodes.codes_set_double(gid, 'longitudeOfLastGridPointInDegrees', -60.0)
        
        # Set data values
        eccodes.codes_set_double_array(gid, 'values', data.flatten())
        
        # Write to file
        eccodes.codes_write(gid, f)
        eccodes.codes_release(gid)
    
    return filepath


def create_synthetic_grib_like_data(
    lat_size: int = 3500,
    lon_size: int = 7000,
    num_files: int = 5,
    format: str = "netcdf"
) -> List[Path]:
    """
    Create synthetic data files that mimic MRMS GRIB2 data structure.
    
    Supports both NetCDF and GRIB2 formats.
    
    MRMS grids are typically:
    - Latitude: ~3500 points (20-55°N at 0.01° resolution)
    - Longitude: ~7000 points (-130 to -60°W at 0.01° resolution)
    - File size: ~100-200MB per uncompressed field
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="benchmark_"))
    files = []
    
    # Create latitude/longitude arrays similar to MRMS
    lats = np.linspace(20.0, 55.0, lat_size)
    lons = np.linspace(-130.0, -60.0, lon_size)
    
    for i in range(num_files):
        # Create random data with some realistic patterns
        data = np.random.random((lat_size, lon_size)).astype(np.float32) * 50
        
        # Add some "storm cells" - high value regions
        for _ in range(20):
            cy = np.random.randint(500, lat_size - 500)
            cx = np.random.randint(500, lon_size - 500)
            radius = np.random.randint(20, 100)
            intensity = np.random.random() * 30 + 40
            
            y, x = np.ogrid[-radius:radius, -radius:radius]
            mask = x**2 + y**2 <= radius**2
            
            y_start = max(0, cy - radius)
            y_end = min(lat_size, cy + radius)
            x_start = max(0, cx - radius)
            x_end = min(lon_size, cx + radius)
            
            y_slice = slice(y_start - cy + radius, y_end - cy + radius)
            x_slice = slice(x_start - cx + radius, x_end - cx + radius)
            
            if mask[y_slice, x_slice].shape == (y_end - y_start, x_end - x_start):
                data[y_start:y_end, x_start:x_end][mask[y_slice, x_slice]] = intensity
        
        if format.lower() == "grib2" and ECCODES_AVAILABLE:
            # Create GRIB2 file
            filepath = temp_dir / f"synthetic_mrms_{i:02d}.grib2"
            create_synthetic_grib_file(filepath, lat_size, lon_size)
            if filepath.exists():
                files.append(filepath)
                print(f"Created synthetic GRIB2 file: {filepath}")
                print(f"  Size: {filepath.stat().st_size / (1024*1024):.1f} MB")
                print(f"  Shape: ({lat_size}, {lon_size})")
        else:
            # Create NetCDF file
            ds = xr.Dataset(
                {
                    "unknown": (["latitude", "longitude"], data)
                },
                coords={
                    "latitude": (["latitude"], lats),
                    "longitude": (["longitude"], lons),
                }
            )
            
            filepath = temp_dir / f"synthetic_mrms_{i:02d}.nc"
            ds.to_netcdf(filepath)
            files.append(filepath)
            
            print(f"Created synthetic NetCDF file: {filepath}")
            print(f"  Size: {filepath.stat().st_size / (1024*1024):.1f} MB")
            print(f"  Shape: {data.shape}")
    
    return files, temp_dir


def create_test_storm_cells(num_cells: int = 100) -> List[Dict[str, Any]]:
    """Create synthetic storm cell data."""
    cells = []
    
    for i in range(num_cells):
        # Random location within the grid bounds
        lat = np.random.uniform(25.0, 50.0)
        lon = np.random.uniform(-120.0, -70.0)
        
        # Create bounding box around centroid
        size = np.random.uniform(0.1, 0.5)
        bbox = [
            [lat - size/2, lon - size/2],
            [lat - size/2, lon + size/2],
            [lat + size/2, lon + size/2],
            [lat + size/2, lon - size/2],
        ]
        
        cell = {
            "id": f"cell_{i:04d}",
            "centroid": [lat, lon],
            "bbox": bbox,
            "properties": {}
        }
        cells.append(cell)
    
    return cells


# =============================================================================
# Current Implementation (Eager Loading)
# =============================================================================

class EagerIntegrator:
    """Current implementation with eager loading."""
    
    def integrate_multi_stats(self, dataset_path: Path, storm_cells: List[Dict], 
                              stats_config_list: List[Dict]) -> List[Dict]:
        """Current implementation that loads entire dataset."""
        if not storm_cells:
            return storm_cells
        
        # Load entire dataset into memory
        ds = xr.open_dataset(dataset_path, decode_timedelta=True)
        ds.load()  # <-- This is the expensive operation
        
        # Coordinates
        lat_name = "latitude" if "latitude" in ds.coords else "lat"
        lon_name = "longitude" if "longitude" in ds.coords else "lon"
        lat_vals = ds[lat_name].values
        lon_vals = ds[lon_name].values
        
        var_name = list(ds.data_vars)[0]
        var = ds.get(var_name)
        var_values = var.values
        
        for cell in storm_cells:
            if "properties" not in cell:
                cell["properties"] = {}
            target = cell["properties"]
            
            # Create polygon from bbox
            poly = self._create_cell_polygon(cell)
            if poly is None:
                for conf in stats_config_list:
                    target[conf['key']] = 0
                continue
            
            try:
                minx, miny, maxx, maxy = poly.bounds
                
                # Find indices
                lat_start_idx = max(0, np.searchsorted(lat_vals, miny))
                lat_end_idx = min(len(lat_vals), np.searchsorted(lat_vals, maxy, side='right'))
                lon_start_idx = max(0, np.searchsorted(lon_vals, minx))
                lon_end_idx = min(len(lon_vals), np.searchsorted(lon_vals, maxx, side='right'))
                
                lat_subset = lat_vals[lat_start_idx:lat_end_idx]
                lon_subset = lon_vals[lon_start_idx:lon_end_idx]
                
                if lat_subset.size == 0 or lon_subset.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                    continue
                
                sub_var = var_values[lat_start_idx:lat_end_idx, lon_start_idx:lon_end_idx]
                sub_lon, sub_lat = np.meshgrid(lon_subset, lat_subset)
                
                if sub_var.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                    continue
                
                inside = sv.contains(poly, sub_lon, sub_lat)
                masked_vals = sub_var[inside]
                masked_vals = masked_vals[~np.isnan(masked_vals)]
                masked_vals = masked_vals[masked_vals >= 0]
                
                if masked_vals.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                else:
                    for conf in stats_config_list:
                        method = conf.get('method', 'max')
                        key = conf['key']
                        
                        if method == "max":
                            target[key] = float(np.max(masked_vals))
                        elif method == "mean":
                            target[key] = float(np.mean(masked_vals))
                        elif method == "percentile":
                            percentile = conf.get('percentile', 90)
                            target[key] = float(np.percentile(masked_vals, percentile))
            
            except Exception as e:
                for conf in stats_config_list:
                    target[conf['key']] = 0
        
        ds.close()
        del ds
        gc.collect()
        
        return storm_cells
    
    @staticmethod
    def _create_cell_polygon(cell):
        """Create polygon from cell bbox."""
        if 'bbox' in cell and cell['bbox'] and len(cell['bbox']) >= 3:
            coords = [(pt[1], pt[0]) for pt in cell['bbox']]
            return Polygon(coords)
        return None


# =============================================================================
# Lazy Loading Implementation
# =============================================================================

class LazyIntegrator:
    """Optimized implementation with lazy loading."""
    
    def integrate_multi_stats(self, dataset_path: Path, storm_cells: List[Dict], 
                              stats_config_list: List[Dict]) -> List[Dict]:
        """Lazy implementation that only loads needed subsets."""
        if not storm_cells:
            return storm_cells
        
        # Open dataset WITHOUT loading into memory
        ds = xr.open_dataset(dataset_path, decode_timedelta=True)
        # No ds.load() call - keep it lazy
        
        # Coordinates - these are small, load them
        lat_name = "latitude" if "latitude" in ds.coords else "lat"
        lon_name = "longitude" if "longitude" in ds.coords else "lon"
        lat_vals = ds[lat_name].values  # Coordinate arrays are small
        lon_vals = ds[lon_name].values
        
        var_name = list(ds.data_vars)[0]
        var = ds.get(var_name)
        
        for cell in storm_cells:
            if "properties" not in cell:
                cell["properties"] = {}
            target = cell["properties"]
            
            poly = self._create_cell_polygon(cell)
            if poly is None:
                for conf in stats_config_list:
                    target[conf['key']] = 0
                continue
            
            try:
                minx, miny, maxx, maxy = poly.bounds
                
                # Find indices
                lat_start_idx = max(0, np.searchsorted(lat_vals, miny))
                lat_end_idx = min(len(lat_vals), np.searchsorted(lat_vals, maxy, side='right'))
                lon_start_idx = max(0, np.searchsorted(lon_vals, minx))
                lon_end_idx = min(len(lon_vals), np.searchsorted(lon_vals, maxx, side='right'))
                
                if lat_start_idx >= lat_end_idx or lon_start_idx >= lon_end_idx:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                    continue
                
                # LAZY: Only load the subset we need using isel
                sub_var = var.isel(
                    {lat_name: slice(lat_start_idx, lat_end_idx),
                     lon_name: slice(lon_start_idx, lon_end_idx)}
                ).compute()  # <-- Only now load the small subset
                
                lat_subset = lat_vals[lat_start_idx:lat_end_idx]
                lon_subset = lon_vals[lon_start_idx:lon_end_idx]
                
                sub_lon, sub_lat = np.meshgrid(lon_subset, lat_subset)
                
                if sub_var.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                    continue
                
                inside = sv.contains(poly, sub_lon, sub_lat)
                masked_vals = sub_var.values[inside]
                masked_vals = masked_vals[~np.isnan(masked_vals)]
                masked_vals = masked_vals[masked_vals >= 0]
                
                if masked_vals.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                else:
                    for conf in stats_config_list:
                        method = conf.get('method', 'max')
                        key = conf['key']
                        
                        if method == "max":
                            target[key] = float(np.max(masked_vals))
                        elif method == "mean":
                            target[key] = float(np.mean(masked_vals))
                        elif method == "percentile":
                            percentile = conf.get('percentile', 90)
                            target[key] = float(np.percentile(masked_vals, percentile))
            
            except Exception as e:
                for conf in stats_config_list:
                    target[conf['key']] = 0
        
        ds.close()
        del ds
        gc.collect()
        
        return storm_cells
    
    @staticmethod
    def _create_cell_polygon(cell):
        """Create polygon from cell bbox."""
        if 'bbox' in cell and cell['bbox'] and len(cell['bbox']) >= 3:
            coords = [(pt[1], pt[0]) for pt in cell['bbox']]
            return Polygon(coords)
        return None


# =============================================================================
# GRIB Fast Loader Implementation (Production)
# =============================================================================

class GribFastIntegrator:
    """Production implementation using the custom GRIB fast loader."""
    
    def integrate_multi_stats(self, dataset_path: Path, storm_cells: List[Dict], 
                              stats_config_list: List[Dict]) -> List[Dict]:
        """Integrate using the fast GRIB loader (eager loading via eccodes)."""
        if not storm_cells:
            return storm_cells
        
        # Use the custom fast loader - loads entire dataset at once
        ds = load_grib_fast(str(dataset_path))
        
        # Coordinates
        lat_name = "latitude" if "latitude" in ds.coords else "lat"
        lon_name = "longitude" if "longitude" in ds.coords else "lon"
        lat_vals = ds[lat_name].values
        lon_vals = ds[lon_name].values
        
        var_name = list(ds.data_vars)[0]
        var = ds.get(var_name)
        var_values = var.values
        
        for cell in storm_cells:
            if "properties" not in cell:
                cell["properties"] = {}
            target = cell["properties"]
            
            poly = self._create_cell_polygon(cell)
            if poly is None:
                for conf in stats_config_list:
                    target[conf['key']] = 0
                continue
            
            try:
                minx, miny, maxx, maxy = poly.bounds
                
                # Find indices
                lat_start_idx = max(0, np.searchsorted(lat_vals, miny))
                lat_end_idx = min(len(lat_vals), np.searchsorted(lat_vals, maxy, side='right'))
                lon_start_idx = max(0, np.searchsorted(lon_vals, minx))
                lon_end_idx = min(len(lon_vals), np.searchsorted(lon_vals, maxx, side='right'))
                
                lat_subset = lat_vals[lat_start_idx:lat_end_idx]
                lon_subset = lon_vals[lon_start_idx:lon_end_idx]
                
                if lat_subset.size == 0 or lon_subset.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                    continue
                
                sub_var = var_values[lat_start_idx:lat_end_idx, lon_start_idx:lon_end_idx]
                sub_lon, sub_lat = np.meshgrid(lon_subset, lat_subset)
                
                if sub_var.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                    continue
                
                inside = sv.contains(poly, sub_lon, sub_lat)
                masked_vals = sub_var[inside]
                masked_vals = masked_vals[~np.isnan(masked_vals)]
                masked_vals = masked_vals[masked_vals >= 0]
                
                if masked_vals.size == 0:
                    for conf in stats_config_list:
                        target[conf['key']] = 0
                else:
                    for conf in stats_config_list:
                        method = conf.get('method', 'max')
                        key = conf['key']
                        
                        if method == "max":
                            target[key] = float(np.max(masked_vals))
                        elif method == "mean":
                            target[key] = float(np.mean(masked_vals))
                        elif method == "percentile":
                            percentile = conf.get('percentile', 90)
                            target[key] = float(np.percentile(masked_vals, percentile))
            
            except Exception as e:
                for conf in stats_config_list:
                    target[conf['key']] = 0
        
        ds.close()
        del ds
        gc.collect()
        
        return storm_cells
    
    @staticmethod
    def _create_cell_polygon(cell):
        """Create polygon from cell bbox."""
        if 'bbox' in cell and cell['bbox'] and len(cell['bbox']) >= 3:
            coords = [(pt[1], pt[0]) for pt in cell['bbox']]
            return Polygon(coords)
        return None


# =============================================================================
# Benchmark Runner
# =============================================================================

def run_benchmark(
    files: List[Path],
    cells: List[Dict],
    stats_config: List[Dict],
    approach: str = "eager"
) -> Dict[str, Any]:
    """Run benchmark for a given approach."""
    
    print(f"\n{'='*60}")
    print(f"Benchmark: {approach.upper()} LOADING")
    print(f"{'='*60}")
    print(f"Files: {len(files)}")
    print(f"Cells: {len(cells)}")
    print(f"Stats per file: {len(stats_config)}")
    
    # Choose integrator
    if approach == "eager":
        integrator = EagerIntegrator()
    elif approach == "grib_fast":
        integrator = GribFastIntegrator()
    else:
        integrator = LazyIntegrator()
    
    # Start memory tracking
    tracemalloc.start()
    start_mem = tracemalloc.get_traced_memory()[0] / (1024 * 1024)
    
    # Run integration
    start_time = time.time()
    
    result_cells = cells
    for filepath in files:
        print(f"  Processing: {filepath.name}")
        result_cells = integrator.integrate_multi_stats(filepath, result_cells, stats_config)
    
    elapsed = time.time() - start_time
    
    # Get peak memory
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    peak_mb = peak / (1024 * 1024)
    current_mb = current / (1024 * 1024)
    
    print(f"\nResults:")
    print(f"  Time: {elapsed:.2f}s")
    print(f"  Peak Memory: {peak_mb:.1f} MB")
    print(f"  Final Memory: {current_mb:.1f} MB")
    
    return {
        "approach": approach,
        "time_seconds": elapsed,
        "peak_memory_mb": peak_mb,
        "final_memory_mb": current_mb,
        "files_processed": len(files),
        "cells_processed": len(cells)
    }


def main():
    """Main benchmark entry point."""
    print("="*60)
    print("LAZY LOADING BENCHMARK")
    print("="*60)
    
    # Configuration
    NUM_FILES = 5
    NUM_CELLS = 100
    GRID_SIZE = (3500, 7000)  # Simulated MRMS grid
    
    print(f"\nConfiguration:")
    print(f"  Synthetic files: {NUM_FILES}")
    print(f"  Grid size: {GRID_SIZE[0]} x {GRID_SIZE[1]}")
    print(f"  Storm cells: {NUM_CELLS}")
    
    # Generate test data
    print("\n" + "-"*60)
    print("Generating synthetic NetCDF test data...")
    nc_files, temp_dir = create_synthetic_grib_like_data(
        lat_size=GRID_SIZE[0],
        lon_size=GRID_SIZE[1],
        num_files=NUM_FILES,
        format="netcdf"
    )
    cells = create_test_storm_cells(NUM_CELLS)
    
    # Stats config (typical MRMS integration config)
    stats_config = [
        {"key": "max_value", "method": "max"},
        {"key": "mean_value", "method": "mean"},
        {"key": "p95_value", "method": "percentile", "percentile": 95},
    ]
    
    # Run benchmarks
    results = []
    
    # NetCDF Eager loading benchmark
    print("\n[1/3] Running NETCDF EAGER loading benchmark...")
    results.append(run_benchmark(nc_files, cells.copy(), stats_config, approach="eager"))
    
    # Force cleanup
    gc.collect()
    time.sleep(1)
    
    # NetCDF Lazy loading benchmark
    print("\n[2/3] Running NETCDF LAZY loading benchmark...")
    results.append(run_benchmark(nc_files, cells.copy(), stats_config, approach="lazy"))
    
    # Summary for NetCDF
    print("\n" + "="*60)
    print("NETCDF BENCHMARK SUMMARY")
    print("="*60)
    
    eager = results[0]
    lazy = results[1]
    
    print(f"\n{'Metric':<25} {'Eager':>15} {'Lazy':>15} {'Improvement':>15}")
    print("-"*75)
    
    time_improvement = ((eager['time_seconds'] - lazy['time_seconds']) / eager['time_seconds']) * 100
    mem_improvement = ((eager['peak_memory_mb'] - lazy['peak_memory_mb']) / eager['peak_memory_mb']) * 100
    
    print(f"{'Time (seconds)':<25} {eager['time_seconds']:>15.2f} {lazy['time_seconds']:>15.2f} {time_improvement:>14.1f}%")
    print(f"{'Peak Memory (MB)':<25} {eager['peak_memory_mb']:>15.1f} {lazy['peak_memory_mb']:>15.1f} {mem_improvement:>14.1f}%")
    
    # GRIB benchmark (if eccodes available)
    if ECCODES_AVAILABLE:
        print("\n" + "="*60)
        print("GRIB2 BENCHMARK (Production Loader)")
        print("="*60)
        print("\nNote: GRIB files require full load via eccodes (no lazy loading)")
        print("This compares the custom fast loader against NetCDF approaches.\n")
        
        # Generate GRIB files
        print("Generating synthetic GRIB2 test data...")
        grib_files, grib_temp_dir = create_synthetic_grib_like_data(
            lat_size=GRID_SIZE[0],
            lon_size=GRID_SIZE[1],
            num_files=NUM_FILES,
            format="grib2"
        )
        
        if grib_files:
            print("\n[3/3] Running GRIB2 FAST loading benchmark...")
            grib_result = run_benchmark(grib_files, cells.copy(), stats_config, approach="grib_fast")
            
            print("\n" + "="*60)
            print("CROSS-FORMAT COMPARISON")
            print("="*60)
            print(f"\n{'Format/Method':<25} {'Time (s)':>15} {'Memory (MB)':>15}")
            print("-"*60)
            print(f"{'NetCDF Eager':<25} {eager['time_seconds']:>15.2f} {eager['peak_memory_mb']:>15.1f}")
            print(f"{'NetCDF Lazy':<25} {lazy['time_seconds']:>15.2f} {lazy['peak_memory_mb']:>15.1f}")
            print(f"{'GRIB2 Fast Loader':<25} {grib_result['time_seconds']:>15.2f} {grib_result['peak_memory_mb']:>15.1f}")
            
            # Cleanup GRIB files
            import shutil
            shutil.rmtree(grib_temp_dir)
    
    print("\n" + "="*60)
    
    # Cleanup
    print("\nCleaning up temporary files...")
    import shutil
    shutil.rmtree(temp_dir)
    print("Done!")
    
    return results


if __name__ == "__main__":
    main()
