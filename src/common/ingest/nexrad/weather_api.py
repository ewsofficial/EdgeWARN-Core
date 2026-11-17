import threading
import logging
import time

import requests

from common.ingest.nexrad.config import (
    station_api_cache_ttl_seconds,
    station_api_timeout_seconds,
    station_catalog_url,
)
from common.ingest.nexrad.models import RadarStationVcp
from util.release import weather_api_headers


def normalize_weather_vcp(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value

    text = str(value).strip().upper()
    if not text:
        return None
    if text.startswith("VCP-"):
        text = text[4:]
    if text.startswith("R"):
        text = text[1:]
    return int(text) if text.isdigit() else None


def _build_station_record(feature: dict) -> RadarStationVcp | None:
    properties = feature.get("properties") or {}
    site = properties.get("id")
    if not site:
        return None

    latency_properties = properties.get("latency") or {}
    rda = properties.get("rda") or {}
    rda_properties = ((properties.get("rda") or {}).get("properties") or {})
    raw_vcp = rda_properties.get("volumeCoveragePattern")
    return RadarStationVcp(
        site=str(site).upper(),
        vcp=normalize_weather_vcp(raw_vcp),
        raw_vcp=raw_vcp,
        rda_timestamp=rda.get("timestamp") or rda_properties.get("timestamp"),
        level_two_last_received_time=(
            latency_properties.get("levelTwoLastReceivedTime")
            or properties.get("timeOfLastLevelTwoVolumeScan")
        ),
        properties=properties,
    )


class RadarStationCatalog:
    """Radar station catalog reader.

    Every configured value is resolved on use rather than in ``__init__``,
    because ``_DEFAULT_CATALOG`` below is constructed at import time -- before
    ``EDGEWARN_CONFIG_DIR`` is exported -- so anything resolved in the
    constructor would freeze the repo default. An explicit argument still wins;
    ``None`` means "ask the catalog now".
    """

    def __init__(
        self,
        *,
        url=None,
        user_agent=None,
        timeout_seconds=None,
        cache_ttl_seconds=None,
    ):
        self._url = url
        self.user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_lock = threading.Lock()
        self._station_cache: dict[str, RadarStationVcp] = {}
        self._station_cache_expires_at = 0.0

    @property
    def url(self) -> str:
        return station_catalog_url() if self._url is None else self._url

    @property
    def timeout_seconds(self) -> float:
        return (
            station_api_timeout_seconds()
            if self._timeout_seconds is None
            else self._timeout_seconds
        )

    @property
    def cache_ttl_seconds(self) -> float:
        return (
            station_api_cache_ttl_seconds()
            if self._cache_ttl_seconds is None
            else self._cache_ttl_seconds
        )

    def _headers(self) -> dict[str, str]:
        return weather_api_headers(user_agent=self.user_agent)

    def fetch_radar_station_vcps(self, *, session=None) -> dict[str, RadarStationVcp]:
        now = time.time()
        with self._cache_lock:
            if self._station_cache and now < self._station_cache_expires_at:
                return dict(self._station_cache)
        try:
            stations = self._fetch_radar_station_vcps(session=session)
            if not stations:
                raise requests.RequestException("empty radar station catalog")
        except requests.RequestException as exc:
            with self._cache_lock:
                if not self._station_cache:
                    raise
                logging.getLogger(__name__).warning("Using cached radar stations after feed failure: %s", exc)
                self._station_cache_expires_at = now + self.cache_ttl_seconds
                return dict(self._station_cache)
        with self._cache_lock:
            self._station_cache = dict(stations)
            self._station_cache_expires_at = now + self.cache_ttl_seconds
        return stations

    def _fetch_radar_station_vcps(self, *, session=None) -> dict[str, RadarStationVcp]:
        headers = self._headers()
        timeout = self.timeout_seconds
        if session is None:
            response = requests.get(self.url, headers=headers, timeout=timeout)
        else:
            response = session.get(self.url, headers=headers, timeout=timeout)

        response.raise_for_status()
        payload = response.json()
        features = payload.get("features") or []
        stations = {}
        for feature in features:
            station = _build_station_record(feature)
            if station is not None:
                stations[station.site] = station
        return stations

    def get_station_vcp(self, site, *, session=None, cache_ttl_seconds=None) -> RadarStationVcp | None:
        site = str(site).upper()
        now = time.time()
        ttl = self.cache_ttl_seconds if cache_ttl_seconds is None else cache_ttl_seconds

        with self._cache_lock:
            if self._station_cache and now < self._station_cache_expires_at:
                return self._station_cache.get(site)

        stations = self.fetch_radar_station_vcps(session=session)
        expires_at = now + max(ttl, 0)
        with self._cache_lock:
            self._station_cache.clear()
            self._station_cache.update(stations)
            self._station_cache_expires_at = expires_at
            return self._station_cache.get(site)

    def reset_station_vcp_cache(self):
        with self._cache_lock:
            self._station_cache.clear()
            self._station_cache_expires_at = 0.0


_DEFAULT_CATALOG = RadarStationCatalog()


def fetch_radar_station_vcps(session=None) -> dict[str, RadarStationVcp]:
    return _DEFAULT_CATALOG.fetch_radar_station_vcps(session=session)


def get_station_vcp(site, *, cache_ttl_seconds=None, session=None) -> RadarStationVcp | None:
    return _DEFAULT_CATALOG.get_station_vcp(site, session=session, cache_ttl_seconds=cache_ttl_seconds)


def reset_station_vcp_cache():
    _DEFAULT_CATALOG.reset_station_vcp_cache()
