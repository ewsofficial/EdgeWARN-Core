"""Phase 5 delivery contracts for containers and package-command CI."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_container_uses_installed_exec_form_package_command():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    environment = yaml.safe_load(
        (REPO_ROOT / "environment.yml").read_text(encoding="utf-8")
    )["name"]

    assert 'ENTRYPOINT ["edgewarn"]' in dockerfile
    assert 'CMD ["run", "--config-path", "/etc/edgewarn/config"]' in dockerfile
    assert "python src/run_" not in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "pip wheel" in dockerfile
    assert "pip install" in dockerfile
    assert f'/opt/conda/envs/{environment}/bin' in dockerfile
    assert "/opt/conda/envs/EdgeWARN-dev/bin" not in dockerfile
    assert "FROM nws-zones-${EDGEWARN_SYNC_NWS_ZONES} AS nws-zones" in dockerfile
    assert "COPY --from=runtime-build" in dockerfile
    assert "COPY --from=nws-zones" in dockerfile


def test_compose_separates_runtime_and_configuration_mounts():
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["edgewarn"]["build"]["args"] == ["EDGEWARN_SYNC_NWS_ZONES"]

    production_mounts = services["edgewarn"]["volumes"]
    assert "${EDGEWARN_HOST_BASE_DIR:-./EdgeWARN_input}:/var/lib/edgewarn" in production_mounts
    assert services["edgewarn"]["environment"]["EDGEWARN_BASE_DIR"] == "/var/lib/edgewarn"
    assert "./config:/etc/edgewarn/config:ro" in production_mounts
    assert "${EDGEWARN_NWS_ASSETS_DIR:-./assets/nws_zones}:/etc/edgewarn/assets/nws_zones:ro" in production_mounts

    admin = services["edgewarn-configure"]
    assert admin["profiles"] == ["admin"]
    assert admin["command"] == [
        "configure",
        "--config-path",
        "/etc/edgewarn/config",
    ]
    assert "./config:/etc/edgewarn/config:rw" in admin["volumes"]

    zone_sync = services["edgewarn-sync-nws-zones"]
    assert zone_sync["profiles"] == ["admin"]
    assert zone_sync["command"] == [
        "sync-nws-zones", "--apply", "--config-path", "/etc/edgewarn/config"
    ]
    assert "${EDGEWARN_NWS_ASSETS_DIR:-./assets/nws_zones}:/etc/edgewarn/assets/nws_zones:rw" in zone_sync["volumes"]


def test_ci_builds_and_smoke_tests_the_installed_wheel():
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "package-command:" in workflow
    assert "pip wheel" in workflow
    assert "edgewarn --help" in workflow
    assert "edgewarn --version" in workflow
    assert "edgewarn configure" in workflow
    assert "test_run_all_launcher.py" in workflow
