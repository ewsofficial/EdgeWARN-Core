import pytest
import numpy as np
import xarray as xr
from unittest.mock import MagicMock, patch
from EdgeWARN.process.integrate.integrate_glm import integrate_glm

@pytest.fixture
def mock_io_manager():
    return MagicMock()

@pytest.fixture
def glm_file(tmp_path):
    """Create a synthetic GLM NetCDF file."""
    # 5 flashes
    # 1: (34.0, -97.0) - Center of storm
    # 2: (34.01, -97.01) - Inside storm
    # 3: (34.1, -96.9) - Edge
    # 4: (30.0, -90.0) - Far away
    # 5: (35.0, -98.0) - Far away
    
    lats = np.array([34.0, 34.01, 34.1, 30.0, 35.0])
    lons = np.array([-97.0, -97.01, -96.9, -90.0, -98.0])
    energies = np.array([100.0, 50.0, 10.0, 999.0, 999.0])
    
    ds = xr.Dataset(
        {
            "flash_lat": (("number_of_flashes",), lats),
            "flash_lon": (("number_of_flashes",), lons),
            "flash_energy": (("number_of_flashes",), energies)
        }
    )
    
    path = tmp_path / "fake_glm.nc"
    ds.to_netcdf(path)
    return str(path)

@pytest.fixture
def storm_cells():
    # 1. Active Cell: Circle-ish around (34.0, -97.0)
    # Using bbox polygon for simplicity (square)
    # Bounds: 33.9 to 34.1, 262.9 to 263.1 (approx -97.1 to -96.9)
    # Detection stores longitudes in 0-360; the shared polygon helper and GLM
    # loader both normalize them to -180..180 for geometric comparison.
    
    bbox_poly = [
        [33.9, 262.9], [34.1, 262.9], [34.1, 263.1], [33.9, 263.1]
    ]
    
    # 2. Empty Cell (Far away from flashes)
    empty_poly = [
        [40.0, -80.0], [41.0, -80.0], [41.0, -79.0], [40.0, -79.0]
    ]
    
    return [
        {"id": 1, "bbox": bbox_poly, "properties": {}},
        {"id": 2, "bbox": empty_poly, "properties": {}}
    ]

def test_integrate_glm_basic(mock_io_manager, glm_file, storm_cells):
    """Test basic GLM integration."""
    
    with patch("EdgeWARN.process.integrate.integrate_glm.io_manager", mock_io_manager):
        
        results = integrate_glm(storm_cells, glm_file)
        
    assert len(results) == 2
    
    # Cell 1
    c1 = results[0]
    # Should catch Flash 1 (34.0, -97.0) -> Inside
    # Should catch Flash 2 (34.01, -97.01) -> Inside
    # Flash 3 (34.1, -96.9) is ON the boundary. Shapely usually includes?
    # Let's check bbox: 33.9 to 34.1 lat, -97.1 to -96.9 lon.
    # Flash 3 is at (34.1, -96.9) -> Top-Right corner.
    
    # Flash 4, 5 are far.
    
    # Expect count >= 2.
    assert c1['properties']['GLM_FLASH_COUNT'] >= 2
    assert c1['properties']['GLM_TOTAL_ENERGY'] > 0
    
    # Cell 2
    c2 = results[1]
    assert c2['properties']['GLM_FLASH_COUNT'] == 0
    assert c2['properties']['GLM_TOTAL_ENERGY'] == 0.0

def test_glm_bin_size_does_not_change_flash_counts(
    mock_io_manager, glm_file, storm_cells, override_integration_config
):
    """`glm.bin_size_degrees` indexes candidates; the polygon test decides.

    integration.yaml claims the key affects performance only. Nothing enforced
    that, so a bin size an operator considers reasonable could silently drop
    flashes near a bin edge.
    """
    def run(bin_size):
        override_integration_config("glm", "bin_size_degrees", bin_size)
        cells = [dict(cell, properties={}) for cell in storm_cells]
        with patch("EdgeWARN.process.integrate.integrate_glm.io_manager", mock_io_manager):
            return [cell["properties"] for cell in integrate_glm(cells, glm_file)]

    coarse = run(5.0)
    assert coarse == run(1.0) == run(0.25)
    assert coarse[0]["GLM_FLASH_COUNT"] > 0


def test_integrate_glm_no_file(mock_io_manager, storm_cells):
    """Test behavior when file path is None."""
    with patch("EdgeWARN.process.integrate.integrate_glm.io_manager", mock_io_manager):
        results = integrate_glm(storm_cells, None)
    
    mock_io_manager.write_error.assert_called_with("GLM file path not provided to integrate_glm")
    assert results == storm_cells # Unchanged

def test_integrate_glm_missing_vars(mock_io_manager, tmp_path, storm_cells):
    """Test behavior when NetCDF is missing variables."""
    # Create bad file
    ds = xr.Dataset({"bad_var": (("x"), [1,2,3])})
    path = tmp_path / "bad.nc"
    ds.to_netcdf(path)
    
    with patch("EdgeWARN.process.integrate.integrate_glm.io_manager", mock_io_manager):
        results = integrate_glm(storm_cells, str(path))
        
    mock_io_manager.write_error.assert_called()
    assert results == storm_cells
