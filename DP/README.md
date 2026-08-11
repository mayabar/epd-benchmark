# vLLM DP=2 + ZMQ KV-event listener

Namespace: `mayab-dp` on cluster `Kermit US-EAST-01A`.

## What we verified

1. **vLLM DP publishes KV events on `base_port + data_parallel_rank`.** For
   `--kv-events-config '{"endpoint":"tcp://<host>:5557", ...}'` with
   `--data-parallel-size=2`:

   | DP rank | ZMQ endpoint         | vLLM process         |
   |---------|----------------------|----------------------|
   | 0       | tcp://<host>:5557    | `EngineCore_DP0`     |
   | 1       | tcp://<host>:5558    | `EngineCore_DP1`     |

   Confirmed by mapping the two `LISTEN` sockets to their PIDs when vLLM
   binds itself (`endpoint: tcp://*:5557`):

       port=5557 pid=599 cmd=VLLM::EngineCore_DP0
       port=5558 pid=600 cmd=VLLM::EngineCore_DP1

2. **Source of the formula.** `vllm/distributed/kv_events.py`,
   `ZmqEventPublisher.offset_endpoint_port`:

       new_port = base_port + data_parallel_rank

   For `inproc://foo` endpoints the rank becomes an `_dp<rank>` suffix.
   Both the main endpoint and the optional `replay_endpoint` are offset.

3. **ZMQ socket wiring.**
   - vLLM publisher (`kv_events.py::_socket_setup`): `zmq.PUB`, `bind` if the
     endpoint contains `*`, else `connect`.
   - `ghcr.io/llm-d/zmq-listener` (from `llm-d-inference-sim/zmq-listener/listener.py`):
     `zmq.SUB`, `bind("tcp://*:$ZMQ_PORT")`, subscribes to all topics.
   - Two binders would collide, so we make vLLM the **connecter** by dropping
     the `*` from its endpoint. See `vllm-dp-pod.yaml`.

4. **End-to-end.** Sent 6 completion requests to the OpenAI-compat endpoint;
   both listeners logged KV events, proving each rank publishes to its own
   port:

       # listener-rank0 (port 5557) — six event batches from EngineCore_DP0
       2026-07-19 11:35:27,431 ... kv@vllm-dp ...
       2026-07-19 11:35:27,507 ... kv@vllm-dp ...
       ...
       # listener-rank1 (port 5558) — six event batches from EngineCore_DP1
       2026-07-19 11:35:27,682 ... kv@vllm-dp ...
       2026-07-19 11:35:27,703 ... kv@vllm-dp ...
       ...

   The listener's `Failed to parse topic ...` errors are cosmetic: it wants
   `kv@<pod-ip>@<model>` and we published as `kv@vllm-dp`. The frames arrived
   on the intended port either way.

## Files in this folder

- `vllm-dp-pod.yaml` — vLLM DP=2 pod, one GPU per rank, connects out to
  `zmq-listener.mayab-dp.svc.cluster.local:5557`.
- `zmq-listener-dp2.yaml` — a single pod with two `ghcr.io/llm-d/zmq-listener`
  containers (`ZMQ_PORT=5557` and `ZMQ_PORT=5558`) plus a Service that maps
  each port back to the correct container.

## Reproduce

    export KUBECONFIG=/Users/mayab/.kube/CWKubeconfig_kermit_US-EAST-01A
    kubectl create namespace mayab-dp
    kubectl apply -f guides/DP/zmq-listener-dp2.yaml
    kubectl apply -f guides/DP/vllm-dp-pod.yaml

    # wait for both pods to be Ready and for /v1/models to answer

    # fire N distinct prompts (>16 tokens each to guarantee a KV block)
    kubectl exec -n mayab-dp vllm-dp -- curl -s http://127.0.0.1:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -d '{"model":"Qwen/Qwen2.5-1.5B-Instruct",
           "prompt":"The quick brown fox jumps over the lazy dog and then runs into a dense forest filled with pine trees and wild flowers.",
           "max_tokens":16}'

    kubectl logs -n mayab-dp zmq-listener -c listener-rank0
    kubectl logs -n mayab-dp zmq-listener -c listener-rank1

## Where each rank sends its KV events

- vLLM `EngineCore_DP0` → `tcp://zmq-listener:5557` → `listener-rank0`
- vLLM `EngineCore_DP1` → `tcp://zmq-listener:5558` → `listener-rank1`
