"""
Config-driven RAP integration.
Optimized: Zero-grid-memory extraction via eccodes RAPPointExtractor.
"""
import ast
import operator

import util.file as fs
from .config import get_rap_products, output_decimals
from util.grib_loader import RAPPointExtractor

# Transformation functions
TRANSFORMS = {
    "kelvin_to_celsius": lambda x: x - 273.15,
}

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _transform_for(product):
    """Resolve a product's value transform, rejecting names not in TRANSFORMS.

    An unknown name used to fall back to the identity function, so a typo
    published raw values under a converted key -- Kelvin labelled as Celsius.
    """
    name = product.get("transform")
    if name is None:
        return lambda x: x
    try:
        return TRANSFORMS[name]
    except KeyError:
        key = product.get("key") or product.get("key_template")
        raise ValueError(
            f"unknown RAP transform {name!r} for product {key!r}; "
            f"known transforms: {sorted(TRANSFORMS)}"
        ) from None


class _AnyNumber:
    """Stand-in for cell properties, so a formula's grammar can be checked with no data.

    Every name resolves to a nonzero number, which separates the two failure
    modes: an unsupported expression still raises, while a name that is merely
    absent at runtime does not.
    """

    @staticmethod
    def get(_name):
        return 1.0


def _compile_derived(entry):
    """Parse and grammar-check one derived formula before any extraction runs.

    A formula the restricted grammar rejects used to be caught per-field and
    written to every cell as None, so a typo in the catalog published a silently
    empty property instead of failing the run.
    """
    key = entry["key"]
    formula = entry["formula"]
    try:
        expression = ast.parse(formula, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"derived RAP field {key!r} formula {formula!r} does not parse: {e}") from None

    try:
        _evaluate_node(expression, _AnyNumber)
    except ValueError as e:
        raise ValueError(f"derived RAP field {key!r} formula {formula!r} is not evaluable: {e}") from None

    return key, expression


def get_rap_output_roots(config=None):
    """Return the top-level property keys populated by RAP integration."""
    if config is None:
        config = get_rap_products()

    roots = set()

    for product in config.get("products", []):
        key = product.get("key") or product.get("key_template")
        if not key:
            continue
        roots.add(key.split(".", 1)[0])

    for derived in config.get("derived", []):
        key = derived.get("key")
        if key:
            roots.add(key.split(".", 1)[0])

    return roots


def _rap_field_cache_dir():
    """Derived decoded-field cache for the hourly RAP file, under the data root."""
    data_dir = getattr(fs, "DATA_DIR", None)
    if data_dir is None:
        return None
    return data_dir / "cache" / "rap_fields"


def integrate_rap(storm_cells, rap_file_path, io_manager):
    """
    Integrate RAP data into storm cells.
    Uses RAPPointExtractor for efficient point-based data extraction.
    """
    if not storm_cells:
        return storm_cells

    if not rap_file_path:
        io_manager.write_warning("No RAP file path provided")
        return storm_cells

    config = get_rap_products()
    products = config.get("products", [])
    derived = config.get("derived", [])

    # Resolved before extraction so an unknown transform name aborts the run
    # rather than surfacing as untransformed values. Formulas are compiled here
    # for the same reason.
    transforms = [_transform_for(product) for product in products]
    compiled_derived = [_compile_derived(entry) for entry in derived]

    decimals = output_decimals()

    # Prepare cell coordinates
    cell_coords = {}
    for cell in storm_cells:
        cid = cell.get("id")
        centroid = cell.get("centroid", [0, 0])
        cell_coords[cid] = (centroid[0], centroid[1])

    # Run batch extraction
    try:
        extractor = RAPPointExtractor(rap_file_path, cache_dir=_rap_field_cache_dir())
        extracted_data = extractor.extract_batch(products, cell_coords)
        io_manager.write_debug(f"Extracted {len(extracted_data)} keys from RAP")
    except Exception as e:
        io_manager.write_error(f"Failed to extract RAP data: {e}")
        return storm_cells

    # Apply extracted data to cells
    for product, transform_fn in zip(products, transforms):

        if "levels" in product:
            levels = product["levels"]
            key_template = product["key_template"]
            
            for level in levels:
                key = key_template.format(level=level)
                if key in extracted_data:
                    _apply_to_cells(storm_cells, extracted_data[key], key, transform_fn, decimals)
        else:
            key = product["key"]
            if key in extracted_data:
                _apply_to_cells(storm_cells, extracted_data[key], key, transform_fn, decimals)

    for key, expression in compiled_derived:
        _calculate_derived(storm_cells, expression, key, decimals)

    return storm_cells


def _set_nested(root, key, value):
    """Set value in a nested dictionary using dot notation."""
    parts = key.split(".")
    curr = root
    for part in parts[:-1]:
        if part not in curr:
            curr[part] = {}
        elif not isinstance(curr[part], dict):
            # Overwrite if it exists but isn't a dict (shouldn't happen with clean data)
            curr[part] = {}
        curr = curr[part]
    curr[parts[-1]] = value


def _apply_to_cells(storm_cells, cell_values, key, transform_fn, decimals):
    """Apply extracted values to cells."""
    for cell in storm_cells:
        if "properties" not in cell:
            cell["properties"] = {}
            
        cid = cell.get("id")
        val = cell_values.get(cid)
        
        final_val = None
        if val is not None:
            try:
                final_val = round(transform_fn(float(val)), decimals)
            except Exception:
                final_val = None
                
        _set_nested(cell["properties"], key, final_val)


def _calculate_derived(storm_cells, compiled_expression, key, decimals):
    """Calculate derived field from existing properties."""
    for cell in storm_cells:
        props = cell.get("properties", {})
        try:
            value = _safe_eval_formula(compiled_expression, props)
            props[key] = round(value, decimals)
        except Exception:
            props[key] = None


def _safe_eval_formula(expression, variables):
    """Evaluate arithmetic formulas using a restricted AST parser."""
    return _evaluate_node(expression, variables)


def _evaluate_node(node, variables):
    """Recursively evaluate an expression node with strict operator support."""
    if isinstance(node, ast.BinOp):
        operator_fn = _BINARY_OPERATORS.get(type(node.op))
        if operator_fn is None:
            raise ValueError("Unsupported binary operator")

        left = _evaluate_node(node.left, variables)
        right = _evaluate_node(node.right, variables)
        if left is None or right is None:
            raise ValueError("Cannot evaluate formula with missing values")

        return operator_fn(left, right)

    if isinstance(node, ast.UnaryOp):
        operator_fn = _UNARY_OPERATORS.get(type(node.op))
        if operator_fn is None:
            raise ValueError("Unsupported unary operator")

        operand = _evaluate_node(node.operand, variables)
        if operand is None:
            raise ValueError("Cannot evaluate formula with missing values")

        return operator_fn(operand)

    if isinstance(node, ast.Name):
        value = variables.get(node.id)
        if isinstance(value, (int, float)):
            return float(value)
        raise ValueError(f"Invalid variable value for '{node.id}'")

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)

    raise ValueError("Unsupported expression in derived formula")
