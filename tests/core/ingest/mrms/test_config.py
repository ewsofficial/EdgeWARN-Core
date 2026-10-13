from common.ingest.mrms.config import (
    GoesIngestSpec,
    get_check_modifiers,
    get_goes_modifiers,
    get_mrms_modifiers,
    normalize_goes_modifier,
)
import util.file as fs






def test_normalize_goes_modifier_supports_legacy_tuple(tmp_path):
    spec = normalize_goes_modifier(("GLM-L2-LCFA", tmp_path / "GLM"))

    assert spec == GoesIngestSpec("GLM-L2-LCFA", tmp_path / "GLM")


def test_get_mrms_modifiers_includes_echotop50():
    assert ("CONUS", "EchoTop_50_00.50", fs.MRMS_ECHOTOP50_DIR) in get_mrms_modifiers()


def test_get_check_modifiers_contains_only_protected_inputs():
    assert {modifier for _, modifier, _ in get_check_modifiers()} == {
        "MergedReflectivityQCComposite_00.50", "PrecipFlag_00.00", None,
    }


def test_goes_catalog_contains_only_scan_time_glm():
    specs = get_goes_modifiers()
    assert len(specs) == 1
    assert specs[0].is_glm
