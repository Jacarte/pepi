#!/bin/bash
set -euo pipefail
export LC_ALL=C
unset DAGGERVALS_PROCESS_STATS DAGGERVALS_STATS_LOG DAGGERVALS_PARENT_PID
# Time limit of the measured TestEnv/agent: default 1800 s, a positive integer, at most
# 3600 s. It is not exported to the agent.
timeout_seconds=${DAGGERVALS_TASK_TIMEOUT_SECONDS:-1800}
unset DAGGERVALS_TASK_TIMEOUT_SECONDS
if [[ ! "$timeout_seconds" =~ ^[1-9][0-9]{0,8}$ ]]; then
  echo "DAGGERVALS_TASK_TIMEOUT_SECONDS must be a positive integer (got '$timeout_seconds')" >&2
  exit 1
fi
if ((timeout_seconds > 3600)); then timeout_seconds=3600; fi
timeout_marker=/tmp/daggervals-task-timeout
agent_exit_marker=/tmp/daggervals-agent-exit
# The first argument is a nonce so Dagger cannot replay an earlier capture.
setup=$2
parser=$3
report=$4
shift 4
work=$(mktemp -d /tmp/daggervals-net.XXXXXXXX)
process_report=/tmp/daggervals-process.jsonl
capture_pid=
task_pid=
rm -f "$report" "$process_report" "$timeout_marker" "$agent_exit_marker"

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
started=$SECONDS
setsid timeout --foreground --signal=TERM --kill-after=5s "${timeout_seconds}s" \
  bash -e -c '
        source "$1" >&2
        export DAGGERVALS_PROCESS_STATS=1 DAGGERVALS_STATS_LOG="$2"
        # Past this line a failure comes from the agent, not from TestEnv.
        : >"$3"
        echo "daggervals: agent started" >&2
        shift 3
        # Fork after setup so its shell CPU/RSS is not charged to the Pi process.
        (export DAGGERVALS_PARENT_PID=$BASHPID; exec "$@") &
        wait "$!"
    ' measured-task "$setup" "$work/process.jsonl" "$work/agent-started" "$@" &
task_pid=$!
status=0
wait "$task_pid" || status=$?
elapsed=$((SECONDS - started))
stop_task
if ! kill -0 "$capture_pid" 2>/dev/null; then
  echo 'network capture exited before the task (possibly the 256 MiB limit)' >&2
  exit 1
fi
if ! stop_capture; then
  echo 'network capture failed to flush/stop cleanly' >&2
  exit 1
fi
# A task that hit the time limit is data: the workspace is graded as it is and the stats
# are still parsed. 124 is timeout(1); 137 is its KILL after the grace period. Both only count
# once the time limit has elapsed.
# An agent that exits non-zero by itself (a model refusal) is passed on too, with its status in
# the marker; the engine keeps it as data only when the Pi report shows the model answered. A
# TestEnv failure, before the agent started, and an agent command that cannot run (126, 127)
# stay failures.
timed_out=false
agent_status=
if ((elapsed >= timeout_seconds && (status == 124 || status == 137))); then
  echo "measured TestEnv/agent timed out after ${timeout_seconds}s" >&2
  timed_out=true
  status=0
elif ((status != 0 && status != 126 && status != 127)) && [[ -f "$work/agent-started" ]]; then
  echo "daggervals: agent exited with status $status" >&2
  agent_status=$status
  status=0
fi
if ((status != 0)); then
  echo "measured TestEnv/agent failed (exit $status; timeout is 124)" >&2
  exit "$status"
fi
# Parse offline only after capture stops, with its own bounded cleanup.
timeout --signal=TERM --kill-after=5s 60s node "$parser" "$work/traffic.pcap" "$work/tcpdump.log" >"$report"
if [[ -f "$work/process.jsonl" ]]; then mv "$work/process.jsonl" "$process_report"; fi
# An agent process that escaped the kill could have written or removed the markers: only this
# script sets them, last, which narrows that window to these few commands.
rm -f "$timeout_marker" "$agent_exit_marker"
if [[ "$timed_out" == true ]]; then echo "$timeout_seconds" >"$timeout_marker"; fi
if [[ -n "$agent_status" ]]; then echo "$agent_status" >"$agent_exit_marker"; fi
# EXIT removes raw capture/logs before Dagger snapshots the result.
