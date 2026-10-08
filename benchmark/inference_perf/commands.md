
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


### Copying results to local folder

List result folders
kubectl exec -n mayab-aggr llmdbench-harness-launcher -- ls -lrt /requests

# copy one folder
kubectl cp -n mayab-aggr \
  llmdbench-harness-launcher:/requests/inference-perf_1791274305_multimodal_img_mm-img-aggr \
  ./results/inference-perf_1791274305_multimodal_img_mm-img-aggr


## Run the benchmark

./run_only.sh -c config.yaml -o results


### Create graphs
https://github.com/dmitripikus/coordinator-performance/blob/main/pd-comparison-analysis/3Dx8GPU_3Px8GPU_multimedia_active_request_scorer/analysis/make_charts.py


