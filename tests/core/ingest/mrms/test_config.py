from common.ingest.mrms.config import (
    GoesIngestSpec,
    abi_radc_product,
    default_abi_radc_channel_ids,
    get_check_modifiers,
    get_abi_radc_channel_specs,
    get_goes_modifiers,
    get_mrms_modifiers,
    normalize_goes_modifier,
)
import util.file as fs


def test_get_abi_radc_channel_specs_includes_all_channels():
    specs = get_abi_radc_channel_specs(channel_ids=None)

    assert [spec.channel_id for spec in specs] == list(default_abi_radc_channel_ids())
    assert all(spec.product == abi_radc_product() for spec in specs)
    assert all(spec.filename_matcher == rf"(?:_|-)M\d{spec.channel_id}_" for spec in specs)


def test_get_goes_modifiers_defaults_to_glm_plus_all_abi_channels():
    specs = get_goes_modifiers()

    assert specs[0] == GoesIngestSpec("GLM-L2-LCFA", specs[0].outdir)
    assert [spec.channel_id for spec in specs[1:]] == list(default_abi_radc_channel_ids())


def test_normalize_goes_modifier_supports_legacy_tuple(tmp_path):
    spec = normalize_goes_modifier(("GLM-L2-LCFA", tmp_path / "GLM"))

    assert spec == GoesIngestSpec("GLM-L2-LCFA", tmp_path / "GLM")


def test_get_mrms_modifiers_includes_echotop50():
    assert ("CONUS", "EchoTop_50_00.50", fs.MRMS_ECHOTOP50_DIR) in get_mrms_modifiers()


def test_get_check_modifiers_contains_only_protected_inputs():
    assert {modifier for _, modifier, _ in get_check_modifiers()} == {
        "MergedReflectivityQCComposite_00.50", "PrecipFlag_00.00", None,
    }
