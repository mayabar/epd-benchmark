# epd vs e-pd: Kimi-VL, 4 images per request, max concurrency 8

This folder compares two llm-d multimodal deployments of the same model. Both are run with the same
`sglang.bench_serving` workload, over a range of request rates. It is the same test as
`../jobs_qwen_4img_128out_8conc/`, except that the model is `moonshotai/Kimi-VL-A3B-Instruct`
instead of `Qwen/Qwen3-VL-32B-Instruct`.

## Deployments

| Setup | llm-d guide | vLLM pods | Gateway (EPP) |
|-------|-------------|-----------|---------------|
| `epd`  | multimodal **aggregation** | 1 pod handles **E**ncode + **P**refill + **D**ecode | `aggregation-epp` |
| `e-pd` | multimodal **encode-disaggregation** | 2 pods: an **encode-only** pod (vision encoder), plus a **prefill+decode** pod | `e-disaggregation-epp` |

In `e-pd` the encoder pod runs the vision encoder on the images. It sends the embeddings to the
prefill+decode pod over NIXL, using the EC connector. Image encoding then no longer competes with
prefill and decode for the same GPUs.

The vLLM deployments use the same settings as the Qwen run (1 GPU per pod, `--max-model-len=65536`,
`--mm-processor-cache-gb 20`), plus `--trust-remote-code`, which Kimi-VL needs for its tokenizer and
processor. The Qwen deployment specs are saved in `../cluster-setup/live-qwen3-vl-32b-2026-10-10/`.

## Workload

| Parameter | Value |
|-----------|-------|
| Model | `moonshotai/Kimi-VL-A3B-Instruct` (MoE, 16B total / 3B active, MoonViT vision encoder) |
| Benchmark tool | `sglang.bench_serving` (`lmsysorg/sglang:v0.5.14`), backend `sglang-oai-chat`, dataset `image` |
| Images per request | 4, random JPEG, 1080p (1920x1080), about 4.2k vision tokens per request (about 1k per image) |
| Input text length | 128 random tokens (`--random-input-len`) |
| Output length | 128 tokens requested (`--random-output-len`). Runs generated about 65 tokens per request on average. |
| Requests per run | 128 |
| Max concurrency | 8 |
| Seed | 0 |

Kimi-VL's vision encoder produces about half as many tokens per image as Qwen3-VL (about 4.2k vs
8.2k vision tokens per request), so the input token counts of the two runs are not the same.

## Request rates (req/s)

| Setup | 0.2 | 0.5 | 0.8 | 1.0 | 1.5 | 2.0 |
|-------|:---:|:---:|:---:|:---:|:---:|:---:|
| `epd`  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `e-pd` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

## Results (one run per setup and rate)

Mean latencies. The `epd` and `e-pd` job files are identical except for the gateway, and both
setups received exactly the same input (531,968 vision tokens per run) and generated the same
8,283 tokens per run. All 128 requests succeeded in every run.

| Rate (req/s) | E2E epd | E2E e-pd | TTFT epd | TTFT e-pd | TPOT epd | TPOT e-pd |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 0.2 | **0.71 s** | 1.22 s | **0.49 s** | 1.00 s | **3.5 ms** | 3.6 ms |
| 0.5 | **0.74 s** | 1.25 s | **0.50 s** | 1.00 s | **3.7 ms** | 4.0 ms |
| 0.8 | **0.75 s** | 1.34 s | **0.51 s** | 1.05 s | **3.9 ms** | 4.8 ms |
| 1.0 | **0.76 s** | 1.36 s | **0.51 s** | 1.04 s | **4.0 ms** | 5.3 ms |
| 1.5 | **0.79 s** | 1.44 s | **0.51 s** | 1.08 s | **4.5 ms** | 6.1 ms |
| 2.0 | **0.84 s** | 1.61 s | **0.54 s** | 1.14 s | **4.8 ms** | 8.3 ms |

Average requests in flight (`Concurrency` in the logs):

| Rate (req/s) | 0.2 | 0.5 | 0.8 | 1.0 | 1.5 | 2.0 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `epd`  | 0.15 | 0.38 | 0.62 | 0.78 | 1.21 | 1.73 |
| `e-pd` | 0.25 | 0.64 | 1.10 | 1.39 | 2.21 | 3.28 |

**With Kimi-VL, `epd` is faster than `e-pd` at every rate.** This is the opposite of the Qwen3-VL-32B
result, where `e-pd` was ahead from 0.5 req/s on.

- **TTFT:** `e-pd` adds a roughly constant 0.5 s (about 1.0 s vs 0.5 s, so twice as long). The
  median TTFT shows the same gap (about 0.98–1.12 s vs 0.49–0.51 s), so it is not caused by a few
  slow requests.
- **E2E:** `e-pd` is 1.7–1.9x slower. Since each request generates only about 65 tokens, the TTFT gap
  makes up most of the difference.
- **TPOT:** the same at 0.2 req/s, then `e-pd` falls behind as the rate grows (8.3 vs 4.8 ms at
  2 req/s). Because `e-pd` requests take longer, about twice as many are in flight at the same rate,
  so its decode batches are larger.
- **No saturation:** even at 2 req/s, at most about 3 requests are in flight, well below the max
  concurrency of 8. Neither setup is near its limit in this range.

A likely explanation, not measured directly: Kimi-VL's vision encoder (MoonViT, about 0.4B
parameters) is small and fast, so running it on the same GPU costs `epd` little. `e-pd` instead pays
a fixed cost on every request: the request goes to the encoder pod first, and then the image
embeddings are sent over the network to the prefill+decode pod. With Qwen3-VL-32B the encoder work
was large enough that moving it to its own GPU paid off. With Kimi-VL, that fixed cost is bigger than
the time saved. Rates above 2 req/s would show whether `e-pd` catches up once the single `epd` pod
gets busy.

Compared with Qwen3-VL-32B on the same workload (`../jobs_qwen_4img_128out_8conc/`), Kimi-VL is much
faster in both setups (`e-pd` E2E 3–7x lower). It has only 3B active parameters and produces half as
many vision tokens per request.

## Layout

```
epd/ , e-pd/
  r<rate>-t<requests>-in<input>-out<output>-img<images>/
    benchmark-job.yaml   # Kubernetes Job + script that runs the benchmark
    log1.log             # bench_serving output (the "Serving Benchmark Result" block)
    load1.log            # vLLM running/waiting queue sampled once per second
epd_vs_e-pd/             # output of plot_epd_vs_e-pd.py
  e2e_comparison.png, ttft_comparison.png, tpot_comparison.png
  summary.csv            # one row per run: mean E2E / TTFT / TPOT
plot_epd_vs_e-pd.py      # regenerates epd_vs_e-pd/
```

To run all cases, from `benchmark/sglang/`: `./run_test_with_vllm_restarts.py jobs_kimi_4img_128out_8conc 1`.

To regenerate the graphs, run `uv run benchmark/sglang/jobs_kimi_4img_128out_8conc/plot_epd_vs_e-pd.py`.
