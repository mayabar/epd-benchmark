#!/usr/bin/env bash
# E-PD (encode-disaggregated, mayab-diaggr): runs the warmup + rate ladder in config.yaml and
# copies the results to ./results/<experiment id>/. Extra args go to run_only.sh.
set -euo pipefail
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-/Users/mayab/.kube/CWKubeconfig_kermit_US-EAST-01A}

NAMESPACE=mayab-diaggr
TARGETS="decode:8200 encode:8000"   # vLLM pods to wait on between rates: <llm-d.ai/role>:<metrics port>

# run_only.sh cd's into its own directory first, so config/results paths must be absolute.
hook="NAMESPACE=${NAMESPACE}
TARGETS='${TARGETS}'
$(cat ../../common/pre_workload.sh)"

exec ../../common/run_only.sh -c "${PWD}/config.yaml" -o "${PWD}/results" --pre-workload "${hook}" "$@"
