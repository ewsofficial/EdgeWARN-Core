# syntax=docker/dockerfile:1

ARG EDGEWARN_SYNC_NWS_ZONES=false

FROM continuumio/miniconda3:25.3.1-1 AS runtime-build

WORKDIR /tmp/edgewarn-build

# environment.yml remains the only runtime dependency authority.  The wheel is
# built and installed without dependency resolution so pip cannot create a
# second, divergent runtime environment.
COPY environment.yml pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN conda env create --file environment.yml \
    && /opt/conda/envs/EdgeWARN/bin/python -m pip wheel \
        --no-deps --no-build-isolation --wheel-dir /tmp/edgewarn-wheel . \
    && /opt/conda/envs/EdgeWARN/bin/python -m pip install \
        --no-deps /tmp/edgewarn-wheel/edgewarn_core-*.whl \
    && mkdir -p /etc/edgewarn \
    && cp -a config /etc/edgewarn/config \
    && conda clean --all --yes \
    && rm -rf /tmp/edgewarn-build /tmp/edgewarn-wheel

# These two stages are selected by EDGEWARN_SYNC_NWS_ZONES. The enabled stage
# is independent of runtime-build, allowing BuildKit to download zone assets
# while the full runtime Conda environment is being solved.
FROM python:3.13-slim AS nws-zones-false
RUN mkdir -p /opt/edgewarn/nws-zones

FROM python:3.13-slim AS nws-zones-true
WORKDIR /tmp/edgewarn-zone-sync
RUN python -m pip install --no-cache-dir requests pyyaml shapely
COPY package.json ./
COPY src/common/config ./src/common/config
COPY src/common/ingest/nws/config.py src/common/ingest/nws/zone_sync.py ./src/common/ingest/nws/
COPY src/util/__init__.py src/util/release.py ./src/util/
COPY config/nws.yaml config/runtime.yaml ./config/
COPY config/schema/nws.schema.json config/schema/runtime.schema.json ./config/schema/
RUN PYTHONPATH=/tmp/edgewarn-zone-sync/src \
    python -m common.ingest.nws.zone_sync \
        --apply \
        --no-progress \
        --config-dir /tmp/edgewarn-zone-sync/config \
        --assets-dir /opt/edgewarn/nws-zones \
    && test -n "$(find /opt/edgewarn/nws-zones -name zones.json -print -quit)"

FROM nws-zones-${EDGEWARN_SYNC_NWS_ZONES} AS nws-zones

FROM continuumio/miniconda3:25.3.1-1

ARG EDGEWARN_SYNC_NWS_ZONES

COPY --from=runtime-build /opt/conda/envs/EdgeWARN /opt/conda/envs/EdgeWARN
COPY --from=runtime-build /etc/edgewarn/config /etc/edgewarn/config
COPY --from=nws-zones /opt/edgewarn/nws-zones /opt/edgewarn/nws-zones

ENV PATH="/opt/conda/envs/EdgeWARN/bin:${PATH}" \
    EDGEWARN_BASE_DIR="/var/lib/edgewarn" \
    EDGEWARN_BUNDLED_NWS_ZONES_DIR="/opt/edgewarn/nws-zones" \
    EDGEWARN_SYNC_NWS_ZONES="${EDGEWARN_SYNC_NWS_ZONES}"

WORKDIR /opt/edgewarn
VOLUME ["/var/lib/edgewarn"]
STOPSIGNAL SIGTERM

ENTRYPOINT ["edgewarn"]
CMD ["run", "--config-path", "/etc/edgewarn/config"]
