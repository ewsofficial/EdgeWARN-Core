import pytest
from unittest.mock import MagicMock, patch
from EdgeWARN.process.integrate.integrate_rap import integrate_rap
from EdgeWARN.process.integrate.config import get_rap_products


@pytest.fixture
def mock_io_manager():
    io = MagicMock()
    io.write_debug = MagicMock()
    io.write_warning = MagicMock()
    io.write_error = MagicMock()
    return io



@pytest.fixture
def storm_cells():
    # Cell at (35.0, 265.0) -> middle of grid
    return [
        {"id": 1, "centroid": [35.0, 265.0], "properties": {}}
    ]


def test_integrate_rap_basic(mock_io_manager, storm_cells):
    """Test basic RAP integration with isobaric winds."""
    with patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        mock_instance = MockExtractor.return_value
        mock_instance.extract_batch.return_value = {
            "wind_field.u850": {1: 10.0}, "wind_field.v850": {1: 5.0},
            "wind_field.u700": {1: 15.0}, "wind_field.v700": {1: 5.0},
            "wind_field.u500": {1: 25.0}, "wind_field.v500": {1: 5.0},
            "wind_field.u250": {1: 40.0}, "wind_field.v250": {1: 5.0}
        }
        results = integrate_rap(storm_cells, "dummy_path.grib2", mock_io_manager)
        
    cell = results[0]
    props = cell['properties']
    
    # Check isobaric winds (nested in wind_field)
    wind = props['wind_field']
    assert wind['u850'] == 10.0
    assert wind['v850'] == 5.0
    assert wind['u700'] == 15.0
    assert wind['u500'] == 25.0
    assert wind['u250'] == 40.0


def test_integrate_rap_derived_fields(mock_io_manager, storm_cells):
    """Test derived field calculation (dewpoint_depression)."""
    with patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        mock_instance = MockExtractor.return_value
        mock_instance.extract_batch.return_value = {
            "temp_2m": {1: 300.0},
            "dewpoint_2m": {1: 290.0},
            "freezing_level_m": {1: 3500.0}
        }
        results = integrate_rap(storm_cells, "dummy_path.grib2", mock_io_manager)
        
    cell = results[0]
    props = cell['properties']
    
    # temp_2m and dewpoint_2m should be in Celsius
    assert props.get('temp_2m') == pytest.approx(26.85, abs=0.1)
    assert props.get('dewpoint_2m') == pytest.approx(16.85, abs=0.1)
    
    # dewpoint_depression = temp_2m - dewpoint_2m = 10.0
    assert props.get('dewpoint_depression') == pytest.approx(10.0, abs=0.1)
    
    # freezing_level_height = freezing_level_m / 1000 = 3.5
    assert props.get('freezing_level_height') == pytest.approx(3.5, abs=0.1)


def test_output_decimals_is_live_for_extracted_and_derived_fields(
    mock_io_manager, storm_cells, override_integration_config
):
    """Raising `output.decimals` must change both the applied and derived values.

    RAP rounds at two separate sites, and both used to hardcode 2, so an
    assertion at the shipped precision could not tell them apart from a wired key.
    """
    override_integration_config("output", "decimals", 4)

    with patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        MockExtractor.return_value.extract_batch.return_value = {
            "temp_2m": {1: 300.123456},
            "freezing_level_m": {1: 3512.3456},
        }
        results = integrate_rap(storm_cells, "dummy_path.grib2", mock_io_manager)

    props = results[0]["properties"]
    assert props["temp_2m"] == 26.9735
    assert props["freezing_level_height"] == 3.5123


def test_integrate_rap_no_file(mock_io_manager, storm_cells):
    """Test no file path returns unchanged cells."""
    results = integrate_rap(storm_cells, None, mock_io_manager)
    assert results == storm_cells
    mock_io_manager.write_warning.assert_called()


def test_integrate_rap_load_fail(mock_io_manager, storm_cells):
    """Test dataset load failure."""
    with patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        mock_instance = MockExtractor.return_value
        mock_instance.extract_batch.side_effect = Exception("Load failed")
        results = integrate_rap(storm_cells, "bad_path.grib2", mock_io_manager)
        
    assert results == storm_cells
    mock_io_manager.write_error.assert_called()


def test_integrate_rap_empty_datasets(mock_io_manager, storm_cells):
    """Test empty datasets list."""
    with patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        mock_instance = MockExtractor.return_value
        mock_instance.extract_batch.return_value = {}
        results = integrate_rap(storm_cells, "empty.grib2", mock_io_manager)
        
    # Should return unchanged
    assert results == storm_cells


def test_safe_eval_rejects_unsafe_formula(mock_io_manager, storm_cells):
    """A formula the restricted grammar rejects aborts the run.

    It used to be caught per-field and written to every cell as None, so a bad
    formula in the catalog was indistinguishable from a cell that simply had no
    input data. Formulas now come from `integration.yaml`, so this is a config
    error and belongs at startup.
    """
    with patch("EdgeWARN.process.integrate.integrate_rap.get_rap_products") as mock_products, \
         patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        mock_products.return_value = {
            "products": [],
            "derived": [{"formula": "__import__('os').system('echo hi')", "key": "unsafe"}]
        }
        MockExtractor.return_value.extract_batch.return_value = {}

        with pytest.raises(ValueError, match="unsafe"):
            integrate_rap(storm_cells, "dummy_path.grib2", mock_io_manager)

    assert "unsafe" not in storm_cells[0].get("properties", {})


def test_safe_eval_handles_missing_input_value(mock_io_manager, storm_cells):
    """Ensure missing variables in formulas do not crash integration."""
    with patch("EdgeWARN.process.integrate.integrate_rap.get_rap_products") as mock_products, \
         patch("EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor") as MockExtractor:
        mock_products.return_value = {
            "products": [],
            "derived": [{"formula": "temp_2m - dewpoint_2m", "key": "dewpoint_depression"}]
        }
        MockExtractor.return_value.extract_batch.return_value = {}

        results = integrate_rap(storm_cells, "dummy_path.grib2", mock_io_manager)

    assert results[0]["properties"]["dewpoint_depression"] is None


def test_get_rap_products_includes_surface_short_name_aliases():
    products_by_key = {
        product["key"]: product
        for product in get_rap_products()["products"]
        if "key" in product
    }

    assert products_by_key["u10m"]["var_aliases"] == ["u10", "10u", "u"]
    assert products_by_key["v10m"]["var_aliases"] == ["v10", "10v", "v"]
    assert products_by_key["temp_2m"]["var_aliases"] == ["t2m", "2t", "t"]
    assert products_by_key["dewpoint_2m"]["var_aliases"] == ["d2m", "2d", "dpt"]
