#!/bin/bash
set -euo pipefail
export LC_ALL=C
unset DAGGERVALS_PROCESS_STATS DAGGERVALS_STATS_LOG DAGGERVALS_PARENT_PID
# The first argument is a nonce so Dagger cannot replay an earlier capture.
setup=$2
parser=$3
report=$4
shift 4
work=$(mktemp -d /tmp/daggervals-net.XXXXXXXX)
process_report=/tmp/daggervals-process.jsonl
capture_pid=
task_pid=
rm -f "$report" "$process_report"

stop_task() {
  if [[ -n "$task_pid" ]]; then
    kill -TERM -- "-$task_pid" 2>/dev/null || true
    for ((i = 0; i < 50; i++)); do
      kill -0 -- "-$task_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL -- "-$task_pid" 2>/dev/null || true
    wait "$task_pid" 2>/dev/null || true
    task_pid=
  fi
}
stop_capture() {
  local status=0
  [[ -n "$capture_pid" ]] || return 0
  kill -INT "$capture_pid" 2>/dev/null || true
  for ((i = 0; i < 50; i++)); do
    kill -0 "$capture_pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$capture_pid" 2>/dev/null; then
    kill -KILL "$capture_pid" 2>/dev/null || true
    status=1
  fi
  wait "$capture_pid" || status=1
  capture_pid=
  return "$status"
}
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  stop_task
  stop_capture || true
  rm -rf "$work"
  if ((status != 0)); then rm -f "$report" "$process_report"; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Only the capture process gets a file-size limit (256 MiB). Failure is fatal,
# never a silently truncated report. No promiscuous mode or elevated caps.
# A 64 MiB kernel buffer absorbs bursts on slow CI runners and packets are not flushed to disk
# one by one (no -U; SIGINT flushes on stop). Reported packet drops still fail closed.
(
  ulimit -c 0
  ulimit -f 512144
  exec tcpdump -p -i any -B 65536 -s 0 -Z "$(id -un)" -w "$work/traffic.pcap"
) 2>"$work/tcpdump.log" &
capture_pid=$!
ready=false
for ((i = 0; i < 100; i++)); do
  if ! kill -0 "$capture_pid" 2>/dev/null; then break; fi
  if grep -q 'listening on ' "$work/tcpdump.log"; then
    ready=true
    break
  fi
  sleep 0.1
done
if [[ "$ready" != true ]]; then
  cat "$work/tcpdump.log" >&2
  echo 'network capture failed to become ready' >&2
  exit 1
fi

# Sourcing setup allows its cwd and environment to reach the agent. Setup logs
# go to stderr: the exec's stdout remains the agent's answer for the oracle.
setsid timeout --foreground --signal=TERM --kill-after=5s 1800s \
  bash -e -c '
        source "$1" >&2
        export DAGGERVALS_PROCESS_STATS=1 DAGGERVALS_STATS_LOG="$2"
        shift 2
        # Fork after setup so its shell CPU/RSS is not charged to the Pi process.
        (export DAGGERVALS_PARENT_PID=$BASHPID; exec "$@") &
        wait "$!"
    ' measured-task "$setup" "$work/process.jsonl" "$@" &
task_pid=$!
status=0
wait "$task_pid" || status=$?
stop_task
if ! kill -0 "$capture_pid" 2>/dev/null; then
  echo 'network capture exited before the task (possibly the 256 MiB limit)' >&2
  exit 1
fi
if ! stop_capture; then
  echo 'network capture failed to flush/stop cleanly' >&2
  exit 1
fi
if ((status != 0)); then
  echo "measured TestEnv/agent failed (exit $status; timeout is 124)" >&2
  exit "$status"
fi
# Parse offline only after capture stops, with its own bounded cleanup.
timeout --signal=TERM --kill-after=5s 60s node "$parser" "$work/traffic.pcap" "$work/tcpdump.log" >"$report"
if [[ -f "$work/process.jsonl" ]]; then mv "$work/process.jsonl" "$process_report"; fi
# EXIT removes raw capture/logs before Dagger snapshots the result.
