#!/usr/bin/env bash

set -u

log_dir="${EDGEWARN_LOG_DIR:-/var/log/edgewarn}"
pipe_dir="$(mktemp -d /tmp/edgewarn-log.XXXXXX)"
log_pipe="${pipe_dir}/stream"
supervisor_pid=""
logger_pid=""
shutdown_requested=0

cleanup() {
    rm -f "${log_pipe}"
    rmdir "${pipe_dir}" 2>/dev/null || true
}

forward_signal() {
    local signum="$1"
    shutdown_requested=1
    if [[ -n "${supervisor_pid}" ]] && kill -0 "${supervisor_pid}" 2>/dev/null; then
        kill "-${signum}" "${supervisor_pid}" 2>/dev/null || true
    fi
}

wait_for_pid() {
    local pid="$1"
    local status

    while true; do
        wait "${pid}"
        status=$?
        if ! kill -0 "${pid}" 2>/dev/null; then
            return "${status}"
        fi
        # A trapped signal interrupted wait while the child was still alive.
    done
}

trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT
trap cleanup EXIT

mkdir -p "${log_dir}"
mkfifo "${log_pipe}"

# Match the launcher's configuration root before resolving the log budget.
launch_args=("$@")
for ((i=0; i<${#launch_args[@]}; i++)); do
    if [[ ${launch_args[i]} == --config-path || ${launch_args[i]} == --config-dir ]]; then
        export EDGEWARN_CONFIG_DIR="${launch_args[i+1]}"
    fi
done
read -r log_count log_mb < <(python -c 'from common.config.loader import load_config; c=load_config("runtime")["logging"]; print(c["retention_files"], c["max_file_mb"])')
if [[ ! ${log_count:-} =~ ^[1-9][0-9]*$ || ! ${log_mb:-} =~ ^[1-9][0-9]*$ ]]; then
    echo "Cannot resolve validated container log budget" >&2
    exit 1
fi
rotatelogs -n "${log_count}" -L "${log_dir}/edgewarn.current.log" \
    "${log_dir}/edgewarn.log" "${log_mb}M" <"${log_pipe}" &
logger_pid=$!

if (( shutdown_requested )); then
    kill -TERM "${logger_pid}" 2>/dev/null || true
    wait_for_pid "${logger_pid}" || true
    exit 0
fi

"$@" >"${log_pipe}" 2>&1 &
supervisor_pid=$!
if (( shutdown_requested )); then
    forward_signal TERM
fi

supervisor_status=0
wait_for_pid "${supervisor_pid}" || supervisor_status=$?

# The supervisor has closed the final writer after reaping its service groups,
# so rotatelogs receives EOF and can flush before the container exits.
logger_status=0
wait_for_pid "${logger_pid}" || logger_status=$?
if (( supervisor_status == 0 && logger_status != 0 )); then
    exit "${logger_status}"
fi
exit "${supervisor_status}"
