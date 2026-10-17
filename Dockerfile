# syntax=docker/dockerfile:1

FROM continuumio/miniconda3:25.3.1-1 AS runtime-build

WORKDIR /tmp/edgewarn-build

# environment.yml remains the only runtime dependency authority.  The wheel is
# built and installed without dependency resolution so pip cannot create a
# second, divergent runtime environment.
COPY environment.yml pyproject.toml README.md package.json package-lock.json ./
COPY src ./src
COPY config ./config
COPY models ./models
COPY scripts/validate-config.js ./scripts/validate-config.js
RUN conda env create --file environment.yml \
    && /opt/conda/envs/EdgeWARN/bin/python -m pip wheel \
        --no-deps --no-build-isolation --wheel-dir /tmp/edgewarn-wheel . \
    && /opt/conda/envs/EdgeWARN/bin/python -m pip install \
        --no-deps /tmp/edgewarn-wheel/edgewarn_core-*.whl \
    && mkdir -p /etc/edgewarn \
    && cp -a config /etc/edgewarn/config \
    && conda clean --all --yes \
    && rm -rf /tmp/edgewarn-build /tmp/edgewarn-wheel

FROM continuumio/miniconda3:25.3.1-1


COPY --from=runtime-build /opt/conda/envs/EdgeWARN /opt/conda/envs/EdgeWARN
COPY --from=runtime-build /etc/edgewarn/config /etc/edgewarn/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2-utils tini \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/edgewarn

COPY --chmod=0755 docker/edgewarn-entrypoint.sh /usr/local/bin/edgewarn-entrypoint

ENV PATH="/opt/conda/envs/EdgeWARN/bin:${PATH}" \
    EDGEWARN_BASE_DIR="/var/lib/edgewarn" \
    EDGEWARN_CONFIG_DIR="/etc/edgewarn/config" \
    PYTHONUNBUFFERED=1 \
    EDGEWARN_LOG_DIR="/var/log/edgewarn"

# Verify bundled configuration without host mounts.
RUN python -c "from common.config.loader import validate_all_configs; validate_all_configs()"

WORKDIR /opt/edgewarn
VOLUME ["/var/lib/edgewarn", "/var/log/edgewarn"]
STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/edgewarn-entrypoint"]
CMD ["edgewarn", "run", "--config-path", "/etc/edgewarn/config"]
