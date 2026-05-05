# GCP Benchmark VM

This Terraform module creates a single GCE VM for running the Redis DSP benchmark on:

- native `redis-server`
- native Python/`uvicorn`
- no Docker in the hot path

## Recommended Size

Default:

- `n2-standard-8`
- `8 vCPU`
- `32 GB RAM`
- `50 GB pd-ssd`

Why this size:

- it is comfortably above the working set of this demo
- it gives enough CPU headroom to reduce local scheduler noise
- it is a better latency benchmark box than a smaller bursty instance

If you want a cheaper first pass:

- `e2-standard-4`

If you want a smaller baseline instead:

- `n2-standard-4`

## Usage

1. Copy the example vars:

```bash
cp terraform.tfvars.example terraform.tfvars
```

2. Fill in:

- `project_id`
- `ssh_public_key`
- restricted `allowed_ssh_cidrs`
- restricted `allowed_app_cidrs`

3. Apply:

```bash
terraform init
terraform apply
```

4. SSH to the VM:

```bash
gcloud compute ssh redis-dsp-bench --zone us-central1-a --project <project-id>
```

## Benchmark Runbook

On the VM:

```bash
mkdir -p ~/workspace/redis-dsp-demo
cd ~/workspace/redis-dsp-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python - <<'PY'
from pathlib import Path
from data.synthetic import generate_dataset
generate_dataset(
    Path("data/generated/synthetic"),
    num_users=4000,
    num_campaigns=2500,
    num_interactions=120000,
    feature_count=12,
    seed=17,
)
PY
python3 data/load_redis.py --redis-url redis://127.0.0.1:6379/0 --dataset-dir data/generated/synthetic
REDIS_URL=redis://127.0.0.1:6379/0 python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In another shell on the VM:

```bash
source .venv/bin/activate
mkdir -p reports/generated
python3 experiments/benchmark.py --base-url http://127.0.0.1:8000 --dataset-dir data/generated/synthetic --output reports/benchmark_report.md
```

## Notes

- The firewall rules default to open CIDRs in variables only for ease of bootstrapping; restrict them before apply.
- This module uses the default VPC by default.
- `n2-standard-8` is the recommended baseline for this benchmark. The dataset is small, so the goal here is latency stability, not memory capacity.
- In environments with OS Login enabled, prefer `gcloud compute ssh` and `gcloud compute scp` over raw `ssh`/`scp`.
