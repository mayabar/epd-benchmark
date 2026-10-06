#!/usr/bin/env python3
"""Like run_tests.py, but restarts vLLM before every single iteration and walks a
whole tree of test-case folders instead of just one.

Expected layout under <root>:
  <root>/epd/<case>/benchmark-job.yaml     -- aggregated ("epd") test cases
  <root>/e-pd/<case>/benchmark-job.yaml    -- encode-disaggregated ("e-pd") test cases

Each <case> folder must contain exactly one file, benchmark-job.yaml, and nothing
else (dotfiles like .DS_Store are ignored everywhere in the tree) -- the tree is
validated up front and the whole run aborts if any case folder is missing the
manifest or has extra non-dotfile files/subdirectories in it.

Per iteration, for every case folder:
  1. scale the vLLM deployment(s) for that folder's type from 0 to 1 (wait for the
     rollout to report Available)
  2. re-establish this script's own kubectl port-forward to the decode deployment's
     metrics port (the previous forward died with the old pod)
  3. warm up: send WARMUP_COUNT chat-completions requests with 1-2 images each
     through the same gateway the real benchmark uses, discarding the responses --
     nothing about this step is logged or saved
  4. run the same iteration run_tests.py already does: delete leftover job, start the
     queue watcher, kubectl apply the manifest, wait for the pod, stream logs, check
     the end marker, wait for the queue to drain
  5. scale the deployment(s) back down to 0

run_tests.py itself is unmodified; its helpers are imported and reused directly.

"epd" cases restart mm-baseline-nvidia-gpu-vllm-decode in mayab-aggr.
"e-pd" cases restart e-pd-disaggregation-nvidia-gpu-vllm-decode AND
...-encode in mayab-diaggr (together, not serially).

Both deployments' pods are pinned (via nodeSelector) to two hardcoded nodes so a
restart always lands in the same place:
  NODE1 -- the aggregated "epd" pod (4 GPU) OR the disaggregated "pd" (decode) pod
           (4 GPU). Only one of the two is ever scaled up at a time (see the
           mutual-exclusion step below), so NODE1 only needs 4 free GPUs, not 8.
  NODE2 -- the disaggregated "e" (encode) pod (1 GPU)
This script does not pick nodes; it only checks the two hardcoded nodes currently
have enough free GPUs for what's pinned there, and refuses to start otherwise.

Before scaling up either type, this script explicitly scales the OTHER type's
deployment(s) down (and waits for them to disappear) first, so "epd" and "e-pd"
are never running at the same time -- both to keep the comparison between them
clean and because they'd otherwise contend for the same 4 GPUs on NODE1.

Example:
  ./run_test_with_vllm_restarts.py jobs 3
  ./run_test_with_vllm_restarts.py jobs 5 --port-epd 9022 --port-e-pd 9023
"""

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import run_tests as rt

# original nodes 
# NODE1 = "gf2a19e"  # epd pod (4 GPU) OR pd/decode pod (4 GPU) -- never both at once
# node with free GPUs
NODE1 = "g11d5e0"
NODE2 = "g13bc90"  # e/encode pod (1 GPU)

MODEL = "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8"

RESTART_CONFIG = {
    "epd": {
        "namespace": "mayab-aggr",
        # decode-equivalent (and only) deployment; also the port-forward/metrics target
        "deployments": ["mm-baseline-nvidia-gpu-vllm-decode"],
        "remote_port": 8000,
        "gateway_service": "aggregation-epp",
    },
    "e-pd": {
        "namespace": "mayab-diaggr",
        # decode first: it's the port-forward/metrics target
        "deployments": [
            "e-pd-disaggregation-nvidia-gpu-vllm-decode",
            "e-pd-disaggregation-nvidia-gpu-vllm-encode",
        ],
        "remote_port": 8200,
        "gateway_service": "e-disaggregation-epp",
    },
}


def log(msg: str) -> None:
    rt.log(msg)


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def check_secret(namespace: str) -> None:
    res = rt.kubectl("get", "secret", "llm-d-hf-token", "-n", namespace, "-o", "name",
                      check=False, quiet=True)
    if res.returncode != 0:
        sys.exit(f"ERROR: secret llm-d-hf-token not found in namespace {namespace}")


def node_allocatable_gpu(node: str) -> int:
    res = rt.kubectl("get", "node", node, "-o",
                      "jsonpath={.status.allocatable.nvidia\\.com/gpu}", quiet=True)
    return int(res.stdout.strip() or "0")


def node_requested_gpu(node: str) -> int:
    # status.phase=Running excludes finished pods that still have spec.nodeName set
    # but no longer hold any GPU.
    res = rt.kubectl("get", "pods", "-A", "--field-selector",
                      f"spec.nodeName={node},status.phase=Running", "-o", "json", quiet=True)
    pods = json.loads(res.stdout)["items"]
    total = 0
    for pod in pods:
        for container in pod["spec"].get("containers", []):
            qty = container.get("resources", {}).get("requests", {}).get("nvidia.com/gpu")
            if qty:
                total += int(qty)
    return total


def check_node_gpu_capacity(node: str, required: int) -> None:
    allocatable = node_allocatable_gpu(node)
    used = node_requested_gpu(node)
    free = allocatable - used
    log(f"node {node}: {free}/{allocatable} GPUs free (need {required})")
    if free < required:
        sys.exit(
            f"ERROR: node {node} only has {free} free GPUs, need {required}. "
            "This is a shared cluster snapshot -- either wait for capacity to free up "
            "or pick different nodes (NODE1/NODE2 constants in this script)."
        )


def list_cases(type_dir: Path) -> list[Path]:
    # dotfiles (.DS_Store, etc.) are ignored everywhere in this tree
    return sorted(e for e in type_dir.iterdir() if not e.name.startswith("."))


def validate_tree(root: Path) -> None:
    problems: list[str] = []
    for folder_type in RESTART_CONFIG:
        type_dir = root / folder_type
        if not type_dir.is_dir():
            problems.append(f"{type_dir} is missing")
            continue
        cases = list_cases(type_dir)
        if not cases:
            problems.append(f"{type_dir} has no test-case subfolders")
            continue
        for case in cases:
            if not case.is_dir():
                problems.append(f"{case} is not a directory")
                continue
            entries = sorted(case.iterdir())
            visible = [e.name for e in entries if not e.name.startswith(".")]
            if visible != ["benchmark-job.yaml"]:
                problems.append(
                    f"{case} must contain exactly 'benchmark-job.yaml' and nothing else "
                    f"(dotfiles are ignored), found: {[e.name for e in entries] or '(empty)'}"
                )
    if problems:
        sys.exit("ERROR: folder-tree validation failed:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------
# Warmup: a handful of multimodal chat-completions requests through the same
# gateway the real benchmark uses, fired before every iteration and discarded.
#
# Real 1920x1080 photos (benchmark/sglang/warmup-images/*.jpg) are used instead of
# a synthetic pixel so the vision encoder sees realistic-sized input. Even-indexed
# requests embed a local file as a base64 data: URL; odd-indexed requests pass the
# original remote http(s) URL directly, so both code paths get exercised.
# --------------------------------------------------------------------------

WARMUP_IMAGES_DIR = Path(__file__).resolve().parent / "warmup-images"

# (local filename, the public URL it was downloaded from) -- both 1920x1080.
WARMUP_IMAGE_SOURCES = [
    ("img1.jpg", "https://picsum.photos/id/1015/1920/1080.jpg"),
    ("img2.jpg", "https://picsum.photos/id/1025/1920/1080.jpg"),
    ("img3.jpg", "https://picsum.photos/id/1035/1920/1080.jpg"),
    ("img4.jpg", "https://picsum.photos/id/1043/1920/1080.jpg"),
]


def _local_image_data_url(filename: str) -> str:
    path = WARMUP_IMAGES_DIR / filename
    if not path.is_file():
        sys.exit(
            f"ERROR: warmup image {path} is missing. Download the warmup images "
            f"into {WARMUP_IMAGES_DIR} first (see WARMUP_IMAGE_SOURCES in this script)."
        )
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


_WARMUP_LOCAL_URLS = [_local_image_data_url(name) for name, _ in WARMUP_IMAGE_SOURCES]
_WARMUP_REMOTE_URLS = [url for _, url in WARMUP_IMAGE_SOURCES]


def _warmup_payload(model: str, image_count: int, use_remote: bool, rotate: int) -> dict:
    urls = _WARMUP_REMOTE_URLS if use_remote else _WARMUP_LOCAL_URLS
    images = [urls[(rotate + j) % len(urls)] for j in range(image_count)]
    content = [{"type": "text", "text": "Describe this image briefly."}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    return {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 5}


def send_warmup_requests(base_url: str, model: str, count: int, timeout: float) -> None:
    """Fire `count` chat-completions requests (alternating 1 and 2 images, alternating
    local-embedded and remote-URL image references, rotating through all 4 downloaded
    photos) at the gateway and discard the responses -- nothing here is logged to a file."""
    ok = 0
    for i in range(count):
        image_count = 1 if i % 2 == 0 else 2
        use_remote = bool(i % 4 >= 2)  # local, local, remote, remote, local, ...
        body = json.dumps(_warmup_payload(model, image_count, use_remote, rotate=i)).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                ok += 1
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log(f"  warmup request {i + 1}/{count} ({'remote' if use_remote else 'local'} url) failed: {exc}")
    log(f"warmup: {ok}/{count} requests succeeded")


# --------------------------------------------------------------------------
# vLLM restart + this script's own port-forward lifecycle
# --------------------------------------------------------------------------

def get_selector(namespace: str, deployment: str) -> str:
    res = rt.kubectl("get", "deployment", deployment, "-n", namespace, "-o",
                      "jsonpath={.spec.selector.matchLabels}", quiet=True)
    labels = json.loads(res.stdout)
    return ",".join(f"{k}={v}" for k, v in labels.items())


def start_port_forward(namespace: str, resource: str, local_port: int, remote_port: int,
                        timeout: float = 60.0) -> subprocess.Popen:
    """`resource` is a kubectl resource ref, e.g. 'deploy/foo' or 'svc/bar'."""
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, resource, f"{local_port}:{remote_port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"ERROR: kubectl port-forward to {resource} exited early (code {proc.returncode})")
        try:
            with socket.create_connection(("localhost", local_port), timeout=1):
                log(f"port-forward {local_port}->{resource}:{remote_port} ready (pid {proc.pid})")
                return proc
        except OSError:
            time.sleep(0.5)
    proc.terminate()
    sys.exit(f"ERROR: port-forward to {resource} did not become ready within {timeout:.0f}s")


def stop_port_forward(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def other_folder_type(folder_type: str) -> str | None:
    """The other configured folder type, or None if folder_type is the only one
    currently active in RESTART_CONFIG (e.g. one type temporarily commented out)."""
    return next((t for t in RESTART_CONFIG if t != folder_type), None)


def scale_down(namespace: str, deployments: list[str], timeout: float) -> None:
    log(f"scaling down {', '.join(deployments)} in {namespace}")
    for name in deployments:
        rt.kubectl("scale", "deployment", name, "-n", namespace, "--replicas=0")
    for name in deployments:
        selector = get_selector(namespace, name)
        rt.kubectl("wait", "--for=delete", "pod", "-n", namespace, "-l", selector,
                   f"--timeout={timeout:.0f}s", check=False)


def scale_up_and_portforward(namespace: str, deployments: list[str], timeout: float,
                              port_forward_proc: subprocess.Popen | None,
                              metrics_deployment: str, local_port: int, remote_port: int) -> subprocess.Popen:
    if port_forward_proc is not None:
        stop_port_forward(port_forward_proc)

    log(f"scaling up {', '.join(deployments)} in {namespace}")
    for name in deployments:
        rt.kubectl("scale", "deployment", name, "-n", namespace, "--replicas=1")
    for name in deployments:
        rt.kubectl("rollout", "status", "deployment", name, "-n", namespace,
                   f"--timeout={timeout:.0f}s")

    return start_port_forward(namespace, f"deploy/{metrics_deployment}", local_port, remote_port)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_iteration_args(args, namespace: str, metrics_url: str) -> argparse.Namespace:
    return argparse.Namespace(
        namespace=namespace,
        job=args.job,
        manifest=args.manifest,
        metrics_url=metrics_url,
        interval=args.interval,
        end_marker=args.end_marker,
        start_timeout=args.start_timeout,
        drain_timeout=args.drain_timeout,
        delete_timeout=args.delete_timeout,
    )


def run_case(args, folder_type: str, case: Path, pf_proc: subprocess.Popen | None,
             gateway_url: str) -> subprocess.Popen:
    cfg = RESTART_CONFIG[folder_type]
    local_port = args.port_epd if folder_type == "epd" else args.port_e_pd
    metrics_url = f"http://localhost:{local_port}/metrics"
    iter_args = build_iteration_args(args, cfg["namespace"], metrics_url)

    start = args.start_index or rt.next_free_index(case)
    indices = list(range(start, start + args.count))
    if not args.overwrite:
        clashes = [p.name for i in indices for p in (case / f"log{i}.log", case / f"load{i}.log") if p.exists()]
        if clashes:
            sys.exit(f"ERROR: would overwrite {', '.join(clashes)} in {case} -- pass --overwrite or --start-index")

    for index in indices:
        try:
            # mutual exclusion: never let epd and e-pd run at the same time
            # (skipped if only one type is currently active in RESTART_CONFIG)
            other_type = other_folder_type(folder_type)
            if other_type is not None:
                other_cfg = RESTART_CONFIG[other_type]
                scale_down(other_cfg["namespace"], other_cfg["deployments"], args.restart_timeout)
            # scale 0 -> 1
            pf_proc = scale_up_and_portforward(
                cfg["namespace"], cfg["deployments"], args.restart_timeout,
                pf_proc, cfg["deployments"][0], local_port, cfg["remote_port"],
            )
            # 10 (default) throwaway multimodal requests through the real gateway path
            send_warmup_requests(gateway_url, MODEL, args.warmup_count, args.warmup_timeout)
            # run the test, storing log<i>.log / load<i>.log as run_tests.py always has
            rt.preflight_metrics(metrics_url)
            rt.run_iteration(iter_args, case, index)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            log(f"WARNING: iteration {index} for {case} failed ({exc}); skipping to next iteration")
        finally:
            # scale 1 -> 0, whether or not the iteration above succeeded
            scale_down(cfg["namespace"], cfg["deployments"], args.restart_timeout)
    return pf_proc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="root folder containing epd/ and e-pd/ subfolders")
    ap.add_argument("count", type=int, help="number of test iterations to run per test case")
    ap.add_argument("--kubeconfig", help="kubeconfig to use (default: the KUBECONFIG already in the environment)")
    ap.add_argument("--job", default="sglang-bench")
    ap.add_argument("--manifest", default="benchmark-job.yaml")
    ap.add_argument("--port-epd", type=int, default=8022,
                     help="local port forwarded to mm-baseline-nvidia-gpu-vllm-decode:8000 (default: %(default)s)")
    ap.add_argument("--port-e-pd", type=int, default=8023,
                     help="local port forwarded to e-pd-disaggregation-nvidia-gpu-vllm-decode:8200 (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--end-marker", default="All rates complete")
    ap.add_argument("--start-index", type=int)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--start-timeout", type=float, default=900.0)
    ap.add_argument("--drain-timeout", type=float, default=900.0)
    ap.add_argument("--delete-timeout", type=float, default=180.0)
    ap.add_argument("--restart-timeout", type=float, default=1800.0,
                     help="seconds to wait for scale-down/scale-up-to-ready per restart (default: %(default)s)")
    ap.add_argument("--warmup-count", type=int, default=10,
                     help="number of throwaway warmup requests sent before each iteration (default: %(default)s)")
    ap.add_argument("--warmup-timeout", type=float, default=60.0,
                     help="per-request timeout in seconds for warmup requests (default: %(default)s)")
    ap.add_argument("--warmup-port-epd", type=int, default=8032,
                     help="local port forwarded to svc/aggregation-epp:80 for warmup requests (default: %(default)s)")
    ap.add_argument("--warmup-port-e-pd", type=int, default=8033,
                     help="local port forwarded to svc/e-disaggregation-epp:80 for warmup requests (default: %(default)s)")
    args = ap.parse_args()

    if args.count < 1:
        sys.exit("ERROR: count must be >= 1")

    if args.kubeconfig:
        os.environ["KUBECONFIG"] = str(Path(args.kubeconfig).expanduser())

    root: Path = args.root.resolve()
    validate_tree(root)

    for cfg in RESTART_CONFIG.values():
        rt.preflight_cluster(cfg["namespace"])
        check_secret(cfg["namespace"])
    check_node_gpu_capacity(NODE1, 4)
    check_node_gpu_capacity(NODE2, 1)

    began = time.monotonic()
    try:
        for folder_type, cfg in RESTART_CONFIG.items():
            warmup_port = args.warmup_port_epd if folder_type == "epd" else args.warmup_port_e_pd
            # the gateway/EPP pod is never restarted by this script, so one forward
            # per folder type covers every case/iteration of that type.
            gw_proc = start_port_forward(cfg["namespace"], f"svc/{cfg['gateway_service']}",
                                          warmup_port, 80)
            gateway_url = f"http://localhost:{warmup_port}"
            pf_proc = None
            try:
                for case in list_cases(root / folder_type):
                    log(f"===== {folder_type}/{case.name} =====")
                    pf_proc = run_case(args, folder_type, case, pf_proc, gateway_url)
            finally:
                if pf_proc is not None:
                    stop_port_forward(pf_proc)
                stop_port_forward(gw_proc)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    log(f"all done in {(time.monotonic() - began) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
