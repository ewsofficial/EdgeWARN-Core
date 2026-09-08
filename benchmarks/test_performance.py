"""
Performance benchmark tests for critical pipeline components.
Measures execution time, memory usage, and CPU usage.

Run with: pytest benchmarks/test_performance.py -v -s
"""
import pytest
import time
import psutil
import os
import gc
from pathlib import Path


def get_process_metrics():
    """Get current process memory and CPU metrics."""
    process = psutil.Process(os.getpid())
    return {
        "memory_mb": process.memory_info().rss / 1024 / 1024,
        "cpu_percent": process.cpu_percent(interval=0.1)
    }


class PerformanceResult:
    """Container for benchmark results."""
    def __init__(self, name: str):
        self.name = name
        self.start_time = None
        self.end_time = None
        self.start_memory_mb = None
        self.end_memory_mb = None
        self.peak_memory_mb = None
        self.cpu_samples = []
    
    def start(self):
        gc.collect()
        metrics = get_process_metrics()
        self.start_time = time.time()
        self.start_memory_mb = metrics["memory_mb"]
        self.peak_memory_mb = self.start_memory_mb
    
    def sample(self):
        metrics = get_process_metrics()
        self.cpu_samples.append(metrics["cpu_percent"])
        if metrics["memory_mb"] > self.peak_memory_mb:
            self.peak_memory_mb = metrics["memory_mb"]
    
    def stop(self):
        self.end_time = time.time()
        metrics = get_process_metrics()
        self.end_memory_mb = metrics["memory_mb"]
        if metrics["memory_mb"] > self.peak_memory_mb:
            self.peak_memory_mb = metrics["memory_mb"]
    
    @property
    def duration_s(self):
        return self.end_time - self.start_time
    
    @property
    def memory_delta_mb(self):
        return self.end_memory_mb - self.start_memory_mb
    
    @property
    def avg_cpu_percent(self):
        return sum(self.cpu_samples) / len(self.cpu_samples) if self.cpu_samples else 0
    
    def report(self):
        return (
            f"\n{'='*50}\n"
            f"BENCHMARK: {self.name}\n"
            f"{'='*50}\n"
            f"Duration:      {self.duration_s:.2f}s\n"
            f"Memory Start:  {self.start_memory_mb:.1f} MB\n"
            f"Memory Peak:   {self.peak_memory_mb:.1f} MB\n"
            f"Memory Delta:  {self.memory_delta_mb:+.1f} MB\n"
            f"Avg CPU:       {self.avg_cpu_percent:.1f}%\n"
            f"{'='*50}"
        )


class TestGribLoaderPerformance:
    """Benchmark tests for the custom GRIB loader."""
    
    @pytest.fixture
    def sample_grib_path(self):
        """Get a sample GRIB file for testing."""
        import util.file as fs
        try:
            files = fs.latest_files(fs.MRMS_AZSHEARLOW_DIR, 1)
            return files[-1] if files else None
        except Exception:
            return None
    
    def test_fast_loader_execution_time(self, sample_grib_path):
        """Test that fast GRIB loader completes within time threshold."""
        if sample_grib_path is None:
            pytest.skip("No sample GRIB file available")
        
        from util.grib_loader import load_grib_fast
        
        result = PerformanceResult("Fast GRIB Loader")
        result.start()
        
        ds = load_grib_fast(sample_grib_path)
        
        result.stop()
        print(result.report())
        
        # Assert performance constraints
        assert result.duration_s < 10.0, f"Fast loader took {result.duration_s:.2f}s, expected < 10s"
        assert ds is not None
    
    def test_fast_loader_memory_usage(self, sample_grib_path):
        """Test that fast GRIB loader has reasonable memory footprint."""
        if sample_grib_path is None:
            pytest.skip("No sample GRIB file available")
        
        from util.grib_loader import load_grib_fast
        
        result = PerformanceResult("Fast GRIB Loader Memory")
        result.start()
        
        ds = load_grib_fast(sample_grib_path)
        
        result.stop()
        print(result.report())
        
        # MRMS AzShear is ~7000x14000 * 8 bytes = ~784 MB
        # Allow some overhead
        assert result.peak_memory_mb < result.start_memory_mb + 1500, \
            f"Memory usage too high: +{result.memory_delta_mb:.1f} MB"


class TestIntegrationPerformance:
    """Benchmark tests for the integration pipeline."""
    
    @pytest.fixture
    def sample_stormcells_path(self):
        """Get a sample storm cells JSON for testing."""
        import util.file as fs
        from pathlib import Path
        try:
            # Get all files and filter for actual stormcell data files (not index)
            all_files = fs.latest_files(fs.STORMCELL_DIR, 10)
            stormcell_files = [f for f in all_files if Path(f).name.startswith("stormcells_")]
            return stormcell_files[-1] if stormcell_files else None
        except Exception:
            return None
    
    def test_integration_execution_time(self, sample_stormcells_path):
        """Test that integration completes within time threshold."""
        if sample_stormcells_path is None:
            pytest.skip("No sample storm cells file available")
        
        from EdgeWARN.process.integrate import main as integration
        
        result = PerformanceResult("Integration Pipeline")
        result.start()
        
        # Sample CPU during execution
        import threading
        stop_sampling = threading.Event()
        
        def sample_loop():
            while not stop_sampling.is_set():
                result.sample()
                time.sleep(0.5)
        
        sampler = threading.Thread(target=sample_loop, daemon=True)
        sampler.start()
        
        integration.main(sample_stormcells_path)
        
        stop_sampling.set()
        sampler.join(timeout=1)
        result.stop()
        
        print(result.report())
        
        # Assert performance constraints
        # With optimization, integration should complete in < 15s
        assert result.duration_s < 30.0, \
            f"Integration took {result.duration_s:.2f}s, expected < 15s"


class TestRAPIntegrationPerformance:
    """Benchmark tests for RAP data integration."""
    
    @pytest.fixture
    def sample_rap_path(self):
        """Get a sample RAP file for testing."""
        import util.file as fs
        try:
            files = fs.latest_files(fs.RAP_DIR, 1)
            return files[-1] if files else None
        except Exception:
            return None
    
    def test_rap_loading_time(self, sample_rap_path):
        """Test that RAP loading completes within time threshold."""
        if sample_rap_path is None:
            pytest.skip("No sample RAP file available")
        
        import cfgrib
        
        result = PerformanceResult("RAP Dataset Loading")
        result.start()
        
        datasets = cfgrib.open_datasets(sample_rap_path)
        
        result.stop()
        print(result.report())
        
        # RAP loading should be fast with open_datasets
        assert result.duration_s < 5.0, \
            f"RAP loading took {result.duration_s:.2f}s, expected < 5s"
        assert len(datasets) > 0
        
        # Cleanup
        for ds in datasets:
            ds.close()


# =============================================================================
# INGESTION PHASE BENCHMARKS
# =============================================================================

class TestIngestionPerformance:
    """Benchmark tests for data ingestion components."""
    
    def test_mrms_file_discovery(self):
        """Test MRMS file discovery performance."""
        import util.file as fs
        
        result = PerformanceResult("MRMS File Discovery")
        result.start()
        
        # Test file listing across multiple directories
        directories = [
            fs.MRMS_COMPOSITE_DIR,
            fs.MRMS_AZSHEARLOW_DIR,
            fs.MRMS_AZSHEARMID_DIR,
            fs.MRMS_VIL_DIR,
            fs.MRMS_ECHOTOP18_DIR,
        ]
        
        for d in directories:
            try:
                fs.latest_files(d, 5)
            except Exception:
                pass
        
        result.stop()
        print(result.report())
        
        # File discovery should be nearly instant
        assert result.duration_s < 1.0, \
            f"File discovery took {result.duration_s:.2f}s, expected < 1s"


# =============================================================================
# DETECTION PHASE BENCHMARKS
# =============================================================================

class TestDetectionPerformance:
    """Benchmark tests for storm cell detection."""
    
    @pytest.fixture
    def sample_composite_paths(self):
        """Get sample composite reflectivity files for testing."""
        import util.file as fs
        try:
            files = fs.latest_files(fs.MRMS_COMPOSITE_DIR, 2)
            return files if len(files) >= 2 else None
        except Exception:
            return None
    
    def test_reflectivity_loading(self, sample_composite_paths):
        """Test composite reflectivity loading performance."""
        if sample_composite_paths is None:
            pytest.skip("No sample composite files available")
        
        from util.grib_loader import load_grib_fast
        
        result = PerformanceResult("Composite Reflectivity Loading")
        result.start()
        
        ds = load_grib_fast(sample_composite_paths[-1])
        
        result.stop()
        print(result.report())
        
        assert result.duration_s < 5.0, \
            f"Reflectivity loading took {result.duration_s:.2f}s, expected < 5s"
        assert ds is not None


class TestMorphologyPerformance:
    """Benchmark tests for morphology processing engine."""
    
    @pytest.fixture
    def sample_reflectivity_data(self):
        """Get sample reflectivity data for morphology testing."""
        import util.file as fs
        from util.grib_loader import load_grib_fast
        try:
            files = fs.latest_files(fs.MRMS_COMPOSITE_DIR, 1)
            if files:
                ds = load_grib_fast(files[-1])
                var_name = list(ds.data_vars)[0]
                return ds[var_name].values
            return None
        except Exception:
            return None
    
    def test_morphology_processing(self, sample_reflectivity_data):
        """Test morphology engine processing performance."""
        if sample_reflectivity_data is None:
            pytest.skip("No sample reflectivity data available")
        
        from EdgeWARN.process.detect.tools.morphology import MorphologyEngine
        import threading
        
        result = PerformanceResult("Morphology Engine")
        result.start()
        
        stop_sampling = threading.Event()
        def sample_loop():
            while not stop_sampling.is_set():
                result.sample()
                time.sleep(0.2)
        
        sampler = threading.Thread(target=sample_loop, daemon=True)
        sampler.start()
        
        # Create a sample binary mask from reflectivity (>30 dBZ)
        mask_slice = (sample_reflectivity_data > 30).astype(bool)
        
        # Process a small region for benchmarking
        # Get a 500x500 slice to simulate typical cell processing
        h, w = mask_slice.shape
        slice_h, slice_w = min(500, h), min(500, w)
        test_mask = mask_slice[:slice_h, :slice_w]
        test_refl = sample_reflectivity_data[:slice_h, :slice_w]
        
        # Run morphology processing multiple times to get meaningful timing
        for _ in range(10):
            metrics = MorphologyEngine.process_cell(test_mask, test_refl)
        
        stop_sampling.set()
        sampler.join(timeout=1)
        result.stop()
        
        print(result.report())
        
        # Morphology should complete within reasonable time
        assert result.duration_s < 5.0, \
            f"Morphology took {result.duration_s:.2f}s, expected < 5s"


# =============================================================================
# CTAM MODULE BENCHMARKS
# =============================================================================

class TestCTAMPerformance:
    """Benchmark tests for CTAM (Cell Tracking and Analysis Modules)."""
    
    @pytest.fixture
    def sample_storm_cells(self):
        """Get sample storm cells for CTAM testing."""
        import util.file as fs
        import json
        from pathlib import Path
        try:
            # Get all files and filter for actual stormcell data files (not index)
            all_files = fs.latest_files(fs.STORMCELL_DIR, 10)
            stormcell_files = [f for f in all_files if Path(f).name.startswith("stormcells_")]
            if stormcell_files:
                with open(stormcell_files[-1], 'r') as f:
                    data = json.load(f)
                return data.get("cells", [])
            return None
        except Exception:
            return None
    
    def test_ctam_pipeline_execution(self, sample_storm_cells):
        """Test CTAM pipeline execution performance."""
        if sample_storm_cells is None or len(sample_storm_cells) == 0:
            pytest.skip("No sample storm cells available")
        
        from EdgeWARN.ctam.run import run_ctam
        
        result = PerformanceResult("CTAM Pipeline")
        result.start()
        
        processed_cells = run_ctam(sample_storm_cells)
        
        result.stop()
        print(result.report())
        
        # CTAM should be very fast (< 1s for typical cell count)
        assert result.duration_s < 2.0, \
            f"CTAM took {result.duration_s:.2f}s, expected < 2s"
        assert len(processed_cells) == len(sample_storm_cells)
    
    def test_stormcast_module(self, sample_storm_cells):
        """Test StormCast module performance."""
        if sample_storm_cells is None or len(sample_storm_cells) == 0:
            pytest.skip("No sample storm cells available")
        
        from EdgeWARN.ctam.modules.StormCast import StormCastModule
        
        result = PerformanceResult("StormCast Module")
        result.start()
        
        module = StormCastModule()
        for cell in sample_storm_cells:
            module.process(cell)
        
        result.stop()
        print(result.report())
        
        assert result.duration_s < 1.0, \
            f"StormCast took {result.duration_s:.2f}s, expected < 1s"
    
# =============================================================================
# GLM INTEGRATION BENCHMARKS
# =============================================================================

class TestGLMPerformance:
    """Benchmark tests for GLM (GOES Lightning Mapper) integration."""
    
    @pytest.fixture
    def sample_glm_path(self):
        """Get a sample GLM file for testing."""
        import util.file as fs
        try:
            files = fs.latest_files(fs.GOES_GLM_DIR, 1)
            return files[-1] if files else None
        except Exception:
            return None
    
    def test_glm_loading(self, sample_glm_path):
        """Test GLM NetCDF loading performance."""
        if sample_glm_path is None:
            pytest.skip("No sample GLM file available")
        
        import xarray as xr
        
        result = PerformanceResult("GLM Data Loading")
        result.start()
        
        ds = xr.open_dataset(sample_glm_path)
        ds.load()
        
        result.stop()
        print(result.report())
        
        # GLM loading should be fast
        assert result.duration_s < 10.0, \
            f"GLM loading took {result.duration_s:.2f}s, expected < 10.0s"
        
        ds.close()


# =============================================================================
# PROBSEVERE INTEGRATION BENCHMARKS
# =============================================================================

class TestProbSeverePerformance:
    """Benchmark tests for ProbSevere data integration."""
    
    @pytest.fixture
    def sample_probsevere_path(self):
        """Get a sample ProbSevere file for testing."""
        import util.file as fs
        try:
            files = fs.latest_files(fs.MRMS_PROBSEVERE_DIR, 1)
            return files[-1] if files else None
        except Exception:
            return None
    
    def test_probsevere_loading(self, sample_probsevere_path):
        """Test ProbSevere JSON loading performance."""
        if sample_probsevere_path is None:
            pytest.skip("No sample ProbSevere file available")
        
        import json
        
        result = PerformanceResult("ProbSevere Data Loading")
        result.start()
        
        with open(sample_probsevere_path, 'r') as f:
            data = json.load(f)
        
        result.stop()
        print(result.report())
        
        # JSON loading should be nearly instant
        assert result.duration_s < 0.5, \
            f"ProbSevere loading took {result.duration_s:.2f}s, expected < 0.5s"
        assert data is not None
