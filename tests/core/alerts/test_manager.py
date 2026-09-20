import pytest
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import util.file as fs
from EdgeWARN.alerts.schema import AlertPayload
from EdgeWARN.alerts.manager import AlertManager


@pytest.fixture
def override_alerts_dir(tmp_path):
    """Redirect EDGEWARN_ALERTS_IDS_DIR and TS_DIR to a temporary directory for isolation."""
    original_ids = getattr(fs, "EDGEWARN_ALERTS_IDS_DIR", None)
    original_ts = getattr(fs, "EDGEWARN_ALERTS_TS_DIR", None)
    fs.EDGEWARN_ALERTS_IDS_DIR = tmp_path / "EdgeWARN" / "ids"
    fs.EDGEWARN_ALERTS_TS_DIR = tmp_path / "EdgeWARN" / "timestamps"
    fs.EDGEWARN_ALERTS_IDS_DIR.mkdir(parents=True, exist_ok=True)
    fs.EDGEWARN_ALERTS_TS_DIR.mkdir(parents=True, exist_ok=True)
    yield fs.EDGEWARN_ALERTS_IDS_DIR
    if original_ids is not None:
        fs.EDGEWARN_ALERTS_IDS_DIR = original_ids
    if original_ts is not None:
        fs.EDGEWARN_ALERTS_TS_DIR = original_ts


# ------------------------------------------------------------------
# AlertPayload tests
# ------------------------------------------------------------------

class TestAlertPayload:
    def test_to_dict_round_trip(self):
        effective = datetime(2026, 3, 4, 12, 0, 0)
        expiry = effective + timedelta(minutes=30)
        payload = AlertPayload(
            alert_type="severe_weather",
            source="StormProb",
            cell_id="cell_42",
            geometry=[(35.0, -97.0), (35.1, -97.0), (35.1, -97.1)],
            effective_time=effective,
            expiry_time=expiry,
            severity="warning",
            threats={"hail": True},
        )
        d = payload.to_dict()

        assert d["alert_type"] == "severe_weather"
        assert d["source"] == "StormProb"
        assert d["id"] == "id:severe_weather:StormProb:cell_42:2026.03.04.12.00.00"
        assert d["cell_id"] == "cell_42"
        assert d["severity"] == "warning"
        assert d["threats"] == {"hail": True}
        assert d["effective"] == effective.isoformat()
        assert d["expires"] == expiry.isoformat()
        assert d["geometry"] == [(35.0, -97.0), (35.1, -97.0), (35.1, -97.1)]


# ------------------------------------------------------------------
# AlertManager tests
# ------------------------------------------------------------------

class TestAlertManager:
    def test_publish_creates_file(self, override_alerts_dir):
        effective = datetime(2026, 3, 4, 12, 0, 0)
        alert = AlertPayload(
            alert_type="severe_weather",
            source="StormProb",
            cell_id="cell_99",
            geometry=[(35.0, -97.0), (35.1, -97.0)],
            effective_time=effective,
            expiry_time=effective + timedelta(minutes=30),
        )

        result = AlertManager.publish(alert)

        assert result is True
        alert_file = override_alerts_dir / "id_severe_weather_StormProb_cell_99_2026.03.04.12.00.00.json"
        assert alert_file.exists()

        with open(alert_file) as f:
            data = json.load(f)

        assert data["source"] == "StormProb"
        assert data["id"] == "id:severe_weather:StormProb:cell_99:2026.03.04.12.00.00"
        assert data["cell_id"] == "cell_99"
        assert data["alert_type"] == "severe_weather"
        assert data["geometry"] == [[35.0, -97.0], [35.1, -97.0]]

        # Check effective → expires = +30 min
        eff = datetime.fromisoformat(data["effective"])
        exp = datetime.fromisoformat(data["expires"])
        assert exp == eff + timedelta(minutes=30)

    def test_publish_rejects_empty_cell_id(self, override_alerts_dir):
        alert = AlertPayload(
            alert_type="flash_flood",
            source="FloodModule",
            cell_id="",
            geometry=[(35.0, -97.0)],
            effective_time=datetime.now(),
            expiry_time=datetime.now() + timedelta(minutes=60),
        )
        assert AlertManager.publish(alert) is False

    def test_publish_rejects_empty_geometry(self, override_alerts_dir):
        alert = AlertPayload(
            alert_type="flash_flood",
            source="FloodModule",
            cell_id="cell_1",
            geometry=[],
            effective_time=datetime.now(),
            expiry_time=datetime.now() + timedelta(minutes=60),
        )
        assert AlertManager.publish(alert) is False

    def test_publish_many(self, override_alerts_dir):
        now = datetime.now()
        alerts = [
            AlertPayload("severe_weather", "StormProb", f"cell_{i}",
                         [(35.0, -97.0)], now, now + timedelta(minutes=30))
            for i in range(3)
        ]
        count = AlertManager.publish_many(alerts)
        assert count == 3
        assert len(list(override_alerts_dir.glob("*.json"))) == 3

    def test_no_collision_across_sources(self, override_alerts_dir):
        """Two modules alerting on the same cell_id must produce separate files."""
        now = datetime.now()
        a1 = AlertPayload("severe_weather", "StormProb", "cell_x",
                          [(35.0, -97.0)], now, now + timedelta(minutes=30))
        a2 = AlertPayload("flash_flood", "FloodModule", "cell_x",
                          [(36.0, -98.0)], now, now + timedelta(minutes=60))

        AlertManager.publish(a1)
        AlertManager.publish(a2)

        files = sorted(f.name for f in override_alerts_dir.glob("*.json"))
        # we aren't necessarily asserting the exact name except that there's two distinct ones
        assert len(files) == 2

        # Contents are distinct
        formatted_time = now.strftime("%Y.%m.%d.%H.%M.%S")
        with open(override_alerts_dir / f"id_severe_weather_StormProb_cell_x_{formatted_time}.json") as f:
            assert json.load(f)["source"] == "StormProb"
        with open(override_alerts_dir / f"id_flash_flood_FloodModule_cell_x_{formatted_time}.json") as f:
            assert json.load(f)["source"] == "FloodModule"

    # ------------------------------------------------------------------
    # Load tests
    # ------------------------------------------------------------------

    def test_load_existing_alert(self, override_alerts_dir):
        """load() should reconstruct an AlertPayload from disk."""
        effective = datetime(2026, 3, 4, 12, 0, 0)
        original = AlertPayload(
            "severe_weather", "StormProb", "cell_50",
            [(35.0, -97.0)], effective, effective + timedelta(minutes=30),
            threats={"hail": True},
        )
        AlertManager.publish(original)

        # load_by_id mapping
        loaded = AlertManager.load_by_id(original.id)
        assert loaded is not None
        assert loaded.cell_id == "cell_50"
        assert loaded.source == "StormProb"
        assert loaded.threats == {"hail": True}

    def test_load_nonexistent_returns_none(self, override_alerts_dir):
        assert AlertManager.load_by_id("does_not_exist") is None

    def test_load_all_returns_all_sources(self, override_alerts_dir):
        now = datetime.now()
        AlertManager.publish(AlertPayload(
            "severe_weather", "StormProb", "cell_y",
            [(35.0, -97.0)], now, now + timedelta(minutes=30)))
        AlertManager.publish(AlertPayload(
            "flash_flood", "FloodModule", "cell_y",
            [(36.0, -98.0)], now, now + timedelta(minutes=60)))

        all_alerts = AlertManager.load_all("cell_y")
        assert len(all_alerts) == 2
        sources = {a.source for a in all_alerts}
        assert sources == {"StormProb", "FloodModule"}

    def test_load_all_empty_dir(self, override_alerts_dir):
        assert AlertManager.load_all("nonexistent") == []

    def test_load_latest_for_cells_scans_once_and_filters_source(self, override_alerts_dir):
        first = datetime(2026, 3, 4, 12, 0, 0)
        second = first + timedelta(minutes=15)
        alerts = [
            AlertPayload("TSTM", "StormProb", "cell_1", [(35.0, -97.0)],
                         first, first + timedelta(minutes=30)),
            AlertPayload("TSTM", "StormProb", "cell_1", [(35.1, -97.1)],
                         second, second + timedelta(minutes=30)),
            AlertPayload("TSTM", "Other", "cell_1", [(36.0, -98.0)],
                         second, second + timedelta(minutes=30)),
            AlertPayload("TSTM", "StormProb", "cell_2", [(37.0, -99.0)],
                         first, first + timedelta(minutes=30)),
            AlertPayload("TSTM", "StormProb", "ignored", [(38.0, -100.0)],
                         first, first + timedelta(minutes=30)),
        ]
        AlertManager.publish_many(alerts)

        latest = AlertManager.load_latest_for_cells(
            "StormProb", ["cell_1", "cell_2"]
        )

        assert set(latest) == {"cell_1", "cell_2"}
        assert latest["cell_1"].effective_time == second
        assert latest["cell_1"].source == "StormProb"

    def test_load_append_republish(self, override_alerts_dir):
        """Demonstrates the load → modify → republish workflow."""
        now = datetime(2026, 3, 4, 14, 0, 0)
        original = AlertPayload(
            "severe_weather", "StormProb", "cell_77",
            [(35.0, -97.0)], now, now + timedelta(minutes=30),
            threats={"hail": True},
        )
        AlertManager.publish(original)

        # Another module loads, appends a threat, and republishes
        loaded = AlertManager.load_by_id(original.id)
        loaded.threats["tornado"] = True
        AlertManager.publish(loaded)

        # Verify the file now has both threats
        reloaded = AlertManager.load_by_id(original.id)
        assert reloaded.threats == {"hail": True, "tornado": True}

    def test_cleanup_expired_removes_old_files(self, override_alerts_dir):
        """cleanup_expired should remove files with expires < now."""

        # 1. Active alert
        now = datetime.now(timezone.utc)
        active_eff = now - timedelta(minutes=10)
        active_exp = now + timedelta(minutes=20)
        a_active = AlertPayload(
            "severe_weather", "StormProb", "cell_active",
            [(35.0, -97.0)], active_eff, active_exp
        )
        
        # 2. Expired alert
        exp_eff = now - timedelta(minutes=40)
        exp_exp = now - timedelta(minutes=10)
        a_expired = AlertPayload(
            "severe_weather", "StormProb", "cell_expired",
            [(35.0, -97.0)], exp_eff, exp_exp
        )
        
        AlertManager.publish(a_active)
        AlertManager.publish(a_expired)
        
        # Verify both exist
        assert len(list(override_alerts_dir.glob("*.json"))) == 2
        
        # Run cleanup
        count = AlertManager.cleanup_expired()
        
        # Verify only active remains
        assert count == 1
        remaining_files = list(override_alerts_dir.glob("*.json"))
        assert len(remaining_files) == 1
        assert "cell_active" in remaining_files[0].name

    def test_cleanup_expired_keeps_future_expiry_even_if_file_is_old(self, override_alerts_dir):
        """Future-expiring alerts must not be deleted solely due to stale mtime."""
        now = datetime.now(timezone.utc)
        effective = now - timedelta(minutes=30)
        future_expiry = now + timedelta(hours=4)

        alert = AlertPayload(
            "severe_weather", "StormProb", "cell_long_lived",
            [(35.0, -97.0)], effective, future_expiry
        )
        AlertManager.publish(alert)

        alert_file = override_alerts_dir / f"{alert.id.replace(':', '_').replace('/', '_')}.json"
        assert alert_file.exists()

        old_ts = (now - timedelta(hours=3)).timestamp()
        os.utime(alert_file, (old_ts, old_ts))

        deleted = AlertManager.cleanup_expired(max_age_minutes=120)
        assert deleted == 0
        assert alert_file.exists()

    def test_cleanup_expired_deletes_old_file_without_expiry(self, override_alerts_dir):
        """Fallback mtime policy should delete stale files with missing expires."""
        data = {
            "id": "id:severe_weather:StormProb:cell_no_expiry:2026.03.04.12.00.00",
            "alert_type": "severe_weather",
            "source": "StormProb",
            "cell_id": "cell_no_expiry",
            "effective": datetime.now(timezone.utc).isoformat(),
            "geometry": [[35.0, -97.0]],
            "threats": {},
        }
        alert_file = override_alerts_dir / "id_severe_weather_StormProb_cell_no_expiry_2026.03.04.12.00.00.json"
        with open(alert_file, "w") as f:
            json.dump(data, f)

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
        os.utime(alert_file, (old_ts, old_ts))

        deleted = AlertManager.cleanup_expired(max_age_minutes=120)
        assert deleted == 1
        assert not alert_file.exists()

    def test_cleanup_expired_deletes_malformed_old_file_by_mtime_fallback(self, override_alerts_dir):
        """Malformed files should be cleaned up by fallback age policy."""
        bad_file = override_alerts_dir / "malformed.json"
        bad_file.write_text("{ this is invalid json", encoding="utf-8")

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
        os.utime(bad_file, (old_ts, old_ts))

        deleted = AlertManager.cleanup_expired(max_age_minutes=120)
        assert deleted == 1
        assert not bad_file.exists()
