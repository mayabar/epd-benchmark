# epd vs e-pd: 4 images per request, 1024 output tokens, max concurrency 8

This folder compares two llm-d multimodal deployments of the same model. Both are run with the same
`sglang.bench_serving` workload, over a range of request rates. It is the same test as
`../jobs_4img_128out_8conc/`, except that each request asks for 1024 output tokens instead of 128.

## Deployments

| Setup | llm-d guide | vLLM pods | Gateway (EPP) |
|-------|-------------|-----------|---------------|
| `epd`  | multimodal **aggregation** | 1 pod handles **E**ncode + **P**refill + **D**ecode | `aggregation-epp` |
| `e-pd` | multimodal **encode-disaggregation** | 2 pods: an **encode-only** pod (vision encoder), plus a **prefill+decode** pod | `e-disaggregation-epp` |

In `e-pd` the encoder pod runs the vision encoder on the images. It sends the embeddings to the
prefill+decode pod over NIXL, using the EC connector. Image encoding then no longer competes with
prefill and decode for the same GPUs.

## Workload

| Parameter | Value |
|-----------|-------|
| Model | `Qwen/Qwen3-VL-32B-Instruct` |
| Benchmark tool | `sglang.bench_serving` (`lmsysorg/sglang:v0.5.14`), backend `sglang-oai-chat`, dataset `image` |
| Images per request | 4, random JPEG, 1080p (1920x1080), about 8.2k vision tokens per request |
| Input text length | 128 random tokens (`--random-input-len`) |
| Output length | 1024 tokens requested (`--random-output-len`). Runs generated about 499 tokens per request on average (63,835 per run). |
| Requests per run | 128 |
| Max concurrency | 8 |
| Seed | 0 |

## Request rates (req/s)

| Setup | 0.2 | 0.5 | 1.0 | 2.0 |
|-------|:---:|:---:|:---:|:---:|
| `epd`  | – | – | – | – |
| `e-pd` | ✓ | ✓ | ✓ | ✓ |

`epd` has not been run yet: its case folders only contain `benchmark-job.yaml`.

## Results so far (`e-pd`, one run per rate)

| Rate (req/s) | Mean E2E (s) | Mean TTFT (s) | Mean TPOT (ms) |
|:---:|:---:|:---:|:---:|
| 0.2 | 14.46 | 2.01 | 25.6 |
| 0.5 | 19.18 | 2.25 | 34.1 |
| 1.0 | 19.11 | 2.18 | 34.4 |
| 2.0 | 19.33 | 2.28 | 35.2 |

With about 500 output tokens per request, each request takes long enough that the max concurrency
of 8 is reached already at 0.5 req/s. From there on, latency stays flat. TTFT stays around 2 s at
every rate, and almost all of the E2E time is decode (about 500 tokens x 34 ms).

## Layout

```
epd/ , e-pd/
  r<rate>-t<requests>-in<input>-out<output>-img<images>/
    benchmark-job.yaml   # Kubernetes Job + script that ran the benchmark
    log1.log             # bench_serving output (the "Serving Benchmark Result" block)
    load1.log            # vLLM running/waiting queue sampled once per second
epd_vs_e-pd/             # output of plot_epd_vs_e-pd.py
  e2e_comparison.png, ttft_comparison.png, tpot_comparison.png
  summary.csv            # one row per run: mean E2E / TTFT / TPOT
plot_epd_vs_e-pd.py      # regenerates epd_vs_e-pd/
```

The case folder names still say `out128`, copied from the 128-output test, but every
`benchmark-job.yaml` here sets `RANDOM_OUTPUT_LEN=1024`. The folder `epd/r1-t120-...` is set to
128 requests too. Only its name says `t120`.

To regenerate the graphs, run `uv run benchmark/sglang/jobs_4img_1024out_8conc/plot_epd_vs_e-pd.py`.
