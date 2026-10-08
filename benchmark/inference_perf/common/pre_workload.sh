# Pre-workload hook. run_only.sh runs it on the harness pod (kubectl exec ... bash -c) before
# every workload, i.e. before every rate. The scenario's run.sh prepends two variables:
#   NAMESPACE  namespace of the vLLM pods
#   TARGETS    space-separated "<llm-d.ai/role label>:<vLLM metrics port>" list, e.g. "decode:8000"
#
# 1. Patches inference-perf's synthetic PNG generator so every image is unique.
#    inference-perf 0.5.0 draws each image as ONE random solid color (2^24 possibilities), so
#    over thousands of images some repeat and hit vLLM's multimodal processor/encoder cache.
#    The patch makes the first pixel row random bytes (1920x3 bytes of entropy per image).
# 2. Waits until every target vLLM pod reports running == 0 and waiting == 0 for QUIET_SECS
#    seconds in a row, then sleeps COOLDOWN_SECS more, so each rate starts on an idle server.

set -uo pipefail
QUIET_SECS=${QUIET_SECS:-10}
COOLDOWN_SECS=${COOLDOWN_SECS:-30}
MAX_WAIT_SECS=${MAX_WAIT_SECS:-1800}

python3 - <<'PY'
import pathlib
import inference_perf.mediagen.synthesis as synthesis

path = pathlib.Path(synthesis.__file__)
src = path.read_text()
old = "    raw = pixel_row * height\n"
new = '    raw = b"\\x00" + rng.bytes(3 * width) + pixel_row * (height - 1)  # epd-bench patch: unique image\n'
if "epd-bench patch" in src:
    print(f"[pre-workload] unique-image patch already applied ({path})")
elif src.count(old) == 1:
    path.write_text(src.replace(old, new))
    print(f"[pre-workload] unique-image patch applied ({path})")
else:
    print(f"[pre-workload] ERROR: unique-image patch pattern not found in {path} - images may repeat!")
PY

echo "[pre-workload] waiting for vLLM in ${NAMESPACE} to be idle (targets: ${TARGETS})"
start=${SECONDS}
quiet=0
while :; do
  busy=0
  status=""
  for target in ${TARGETS}; do
    role=${target%%:*}
    port=${target##*:}
    ips=$(kubectl get pods -n "${NAMESPACE}" -l "llm-d.ai/role=${role}" --field-selector=status.phase=Running \
          -o jsonpath='{.items[*].status.podIP}' 2>/dev/null)
    if [[ -z "${ips}" ]]; then
      busy=1; status+=" ${role}:no-running-pod"; continue
    fi
    for ip in ${ips}; do
      if ! metrics=$(curl -sf -m 5 "http://${ip}:${port}/metrics"); then
        busy=1; status+=" ${role}@${ip}:unreachable"; continue
      fi
      running=$(awk '/^vllm:num_requests_running[{ ]/ {s += $NF} END {print s + 0}' <<<"${metrics}")
      waiting=$(awk '/^vllm:num_requests_waiting[{ ]/ {s += $NF} END {print s + 0}' <<<"${metrics}")
      status+=" ${role}@${ip}:running=${running},waiting=${waiting}"
      [[ "${running}" == 0 && "${waiting}" == 0 ]] || busy=1
    done
  done

  if (( busy )); then quiet=0; else quiet=$(( quiet + 1 )); fi
  elapsed=$(( SECONDS - start ))
  (( elapsed % 10 == 0 || quiet >= QUIET_SECS )) && echo "[pre-workload] t=${elapsed}s idle=${quiet}s${status}"
  (( quiet >= QUIET_SECS )) && break
  if (( elapsed >= MAX_WAIT_SECS )); then
    echo "[pre-workload] WARNING: vLLM not idle after ${MAX_WAIT_SECS}s - starting the workload anyway"
    break
  fi
  sleep 1
done

echo "[pre-workload] vLLM idle; cooling down ${COOLDOWN_SECS}s before the workload"
sleep "${COOLDOWN_SECS}"
