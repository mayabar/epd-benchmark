
### Scale up deployments

```bash

k scale deployment mm-baseline-nvidia-gpu-vllm-decode --replicas=1 -n mayab-aggr

k scale deployment mm-baseline-nvidia-gpu-vllm-decode --replicas=0 -n mayab-aggr

```

```bash

k scale deployment aggregation-epp --replicas=1 -n mayab-aggr

```


Model Name 
```bash

Qwen/Qwen3-VL-235B-A22B-Instruct-FP8

```

### Setup Aggregated Installation

```bash

k port-forward -n mayab-aggr svc/aggregation-epp 8080:80

k delete pod llmdbench-harness-launcher -n mayab-aggr

```

CURL Example

```bash

curl -i "localhost:8011/v1/completions" -H 'Content-Type: application/json'  -d '{ "model": "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8", "prompt": "say hi", "max_tokens":10}'

```
