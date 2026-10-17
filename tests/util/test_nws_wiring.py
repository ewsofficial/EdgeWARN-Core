"""--disable-nws is parsed for ingest and routed only to the ingest worker."""
import run_all
from util.cli import build_service_parser


def test_ingest_parser_accepts_disable_nws_and_negation():
    parser = build_service_parser("ingest")
    assert parser.parse_args([]).disable_nws is None
    assert parser.parse_args(["--disable-nws"]).disable_nws is True
    assert parser.parse_args(["--no-disable-nws"]).disable_nws is False


def test_disable_nws_routes_to_ingest_only():
    assert run_all._ROUTING["--disable-nws"] == ("ingest",)


def test_preflight_failure_message_names_remedy(monkeypatch):
    from util.runtime.nws_loop import NwsAlertLoop
    import common.ingest.nws.geomapper as geomapper

    def missing():
        raise FileNotFoundError("NWS zone assets missing.")

    monkeypatch.setattr(geomapper, "ensure_zone_assets", missing)
    try:
        NwsAlertLoop.preflight()
    except FileNotFoundError as exc:
        assert "zone assets" in str(exc)
    else:
        raise AssertionError("preflight must propagate missing-asset errors")
