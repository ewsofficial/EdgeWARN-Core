"""
Benchmark comparing brute force vs optimized grid indexing.

This benchmark measures the performance improvement from replacing the
brute force distance calculation in _precompute_cell_indices with the
optimized GridIndex approach.
"""
import time
from pathlib import Path
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from EdgeWARN.process.integrate.grid_index import GridIndex, RegularGridIndexer


def brute_force_precompute_cell_indices(storm_cells, lat_vals, lon_vals):
    """
    Original brute force implementation for comparison.
    This is the old O(N*M) approach.
    """
    indices = {}
    for cell in storm_cells:
        cell_id = cell.get("id")
        centroid = cell.get("centroid", [0, 0])
        lat, lon = centroid[0], centroid[1]
        if lon > 180:
            lon -= 360
        try:
            dist_sq = (lat_vals - lat) ** 2 + (lon_vals - lon) ** 2
            indices[cell_id] = np.unravel_index(np.argmin(dist_sq), dist_sq.shape)
        except Exception:
            indices[cell_id] = None
    return indices


def optimized_precompute_cell_indices(storm_cells, lat_vals, lon_vals):
    """
    New optimized implementation using GridIndex.
    This uses O(1) lookups for regular grids.
    """
    indexer = GridIndex.create(lat_vals, lon_vals)
    
    indices = {}
    for cell in storm_cells:
        cell_id = cell.get("id")
        centroid = cell.get("centroid", [0, 0])
        lat, lon = centroid[0], centroid[1]
        
        try:
            indices[cell_id] = indexer.query(lat, lon)
        except Exception:
            indices[cell_id] = None
    
    return indices


def benchmark_grid_indexing(grid_shape, num_cells, num_runs=10):
    """
    Benchmark grid indexing performance.
    
    Args:
        grid_shape: Tuple of (lat_dim, lon_dim) for grid size
        num_cells: Number of storm cells to query
        num_runs: Number of benchmark runs for averaging
        
    Returns:
        Dictionary with benchmark results
    """
    # Create realistic grid
    lat_dim, lon_dim = grid_shape
    lats = np.linspace(20, 55, lat_dim)
    lons = np.linspace(-140, -50, lon_dim)
    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
    
    # Create random storm cells
    np.random.seed(42)
    storm_cells = [
        {
            "id": i,
            "centroid": [
                np.random.uniform(25, 50),    # lat
                np.random.uniform(-120, -80)  # lon
            ]
        }
        for i in range(num_cells)
    ]
    
    print(f"\nBenchmark Configuration:")
    print(f"  Grid size: {grid_shape[0]} x {grid_shape[1]} = {grid_shape[0] * grid_shape[1]:,} points")
    print(f"  Storm cells: {num_cells}")
    print(f"  Operations (brute force): {num_cells * grid_shape[0] * grid_shape[1]:,} distance calculations")
    print(f"  Runs: {num_runs}")
    
    # Warmup
    brute_force_precompute_cell_indices(storm_cells[:5], lat_grid, lon_grid)
    optimized_precompute_cell_indices(storm_cells[:5], lat_grid, lon_grid)
    
    # Benchmark brute force
    brute_times = []
    for _ in range(num_runs):
        start = time.perf_counter()
        brute_indices = brute_force_precompute_cell_indices(storm_cells, lat_grid, lon_grid)
        end = time.perf_counter()
        brute_times.append(end - start)
    
    brute_time_avg = np.mean(brute_times)
    brute_time_std = np.std(brute_times)
    
    # Benchmark optimized
    opt_times = []
    for _ in range(num_runs):
        start = time.perf_counter()
        opt_indices = optimized_precompute_cell_indices(storm_cells, lat_grid, lon_grid)
        end = time.perf_counter()
        opt_times.append(end - start)
    
    opt_time_avg = np.mean(opt_times)
    opt_time_std = np.std(opt_times)
    
    # Verify results match
    match_count = 0
    mismatch_count = 0
    for cell in storm_cells:
        cell_id = cell["id"]
        brute_idx = brute_indices[cell_id]
        opt_idx = opt_indices[cell_id]
        
        # For grid points, allow small differences due to rounding
        if brute_idx is None or opt_idx is None:
            if brute_idx == opt_idx:
                match_count += 1
            else:
                mismatch_count += 1
        elif abs(brute_idx[0] - opt_idx[0]) <= 1 and abs(brute_idx[1] - opt_idx[1]) <= 1:
            match_count += 1
        else:
            mismatch_count += 1
    
    # Calculate speedup
    speedup = brute_time_avg / opt_time_avg if opt_time_avg > 0 else float('inf')
    
    return {
        'grid_shape': grid_shape,
        'num_cells': num_cells,
        'brute_time_avg': brute_time_avg,
        'brute_time_std': brute_time_std,
        'opt_time_avg': opt_time_avg,
        'opt_time_std': opt_time_std,
        'speedup': speedup,
        'match_count': match_count,
        'mismatch_count': mismatch_count,
        'indexer_type': type(GridIndex.create(lat_grid, lon_grid)).__name__
    }


def print_results(results):
    """Pretty print benchmark results."""
    print(f"\n{'='*60}")
    print(f"Benchmark Results")
    print(f"{'='*60}")
    print(f"Grid: {results['grid_shape'][0]} x {results['grid_shape'][1]} ({results['grid_shape'][0] * results['grid_shape'][1]:,} points)")
    print(f"Cells: {results['num_cells']}")
    print(f"Indexer: {results['indexer_type']}")
    print(f"\nTiming:")
    print(f"  Brute Force: {results['brute_time_avg']*1000:.3f} ± {results['brute_time_std']*1000:.3f} ms")
    print(f"  Optimized:   {results['opt_time_avg']*1000:.3f} ± {results['opt_time_std']*1000:.3f} ms")
    print(f"\nSpeedup: {results['speedup']:.1f}x faster")
    print(f"\nAccuracy:")
    print(f"  Matches: {results['match_count']}/{results['num_cells']}")
    if results['mismatch_count'] > 0:
        print(f"  Mismatches: {results['mismatch_count']}")
    print(f"{'='*60}")


def main():
    """Run benchmarks for different grid sizes."""
    print("\n" + "="*60)
    print("RAP Grid Index Optimization Benchmark")
    print("="*60)
    
    # Test configurations
    configs = [
        # (grid_shape, num_cells)
        ((100, 100), 50),      # Small grid
        ((337, 451), 100),     # RAP-sized grid (from documentation)
        ((500, 700), 200),     # Large MRMS-like grid
    ]
    
    all_results = []
    
    for grid_shape, num_cells in configs:
        results = benchmark_grid_indexing(grid_shape, num_cells, num_runs=10)
        print_results(results)
        all_results.append(results)
    
    # Summary
    print("\n" + "="*60)
    print("Summary")
    print("="*60)
    print(f"{'Grid Size':<20} {'Cells':<8} {'Brute (ms)':<12} {'Optimized (ms)':<15} {'Speedup':<10}")
    print("-"*60)
    for r in all_results:
        grid_str = f"{r['grid_shape'][0]}x{r['grid_shape'][1]}"
        print(f"{grid_str:<20} {r['num_cells']:<8} {r['brute_time_avg']*1000:>11.3f} {r['opt_time_avg']*1000:>14.3f} {r['speedup']:>9.1f}x")
    print("="*60)


if __name__ == "__main__":
    main()
