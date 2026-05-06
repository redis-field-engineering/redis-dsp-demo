# Full-Scale GCP Benchmark — Spec

**Status:** plan only. No infrastructure has been provisioned. Sign off on the sizing numbers below before applying any Terraform.

This spec describes how to validate the prototype against a production-shaped workload: ~500 M MAIDs, ~5 K active ads, AND/OR/NOT taxonomy filters on float scores, target end-to-end bid latency `< 10 ms`, target throughput `100 K → 1 M` bid requests per second.

The plan is to stand up a Redis Enterprise cluster on GCP using the [`redis-field-engineering/terraform-gcp-redis-enterprise`](https://github.com/redis-field-engineering/terraform-gcp-redis-enterprise) Terraform module, configure the database with `redisctl`, drive load from a GCP Managed Instance Group of HTTP load generators, and measure bid-decision latency, ranking quality, and cluster-side throughput against the prototype's `hybrid_bitmap_taxonomy` mode.

---

## 1. Sizing Analysis (Pre-Apply)

These numbers are estimates derived from the workload requirements. They are the basis for how big a cluster to provision. **Re-validate them against measured per-key memory before scaling above ~10% of the target footprint.**

### 1.1 Per-MAID memory

Each MAID profile is shaped roughly as:

- up to 50 publisher-hashed identity tokens per MAID (16-byte hex string each)
- up to 500 taxonomy labels with float scores
- geo (postal_code, city, state, country)
- device (type, os)
- consent (3 booleans)
- card_tier, spend_tier

Two encoding options:

| Encoding | Size per MAID | Notes |
| --- | --- | --- |
| Single JSON STRING (RedisJSON or plain) | ~12 KB | Single key, one network round trip, tighter packing. |
| Flat Redis HASH | ~18 KB | More flexible partial updates (an upstream feedback pipeline can `HSET` one taxonomy label) but per-field overhead is significant at 50+ identity fields and 500 taxonomy fields. |

**Working assumption: ~15 KB average per MAID after encoding overhead.** Validate empirically by loading 1 M MAIDs and dividing `INFO MEMORY` by 1 M.

At 500 M MAIDs: **7.5 TB**.

### 1.2 Identity → MAID resolution

The bid-time path arrives with a publisher-supplied identity hash. The bid engine holds the publisher salt in config, dehashes that to a raw account number in memory, and rederives a canonical `identity_token` — all before any Redis call. Redis only ever sees `identity_token`. Raw account numbers are never written to Redis and never logged.

Because there is one MAID per underlying account in this release, the canonical `identity_token` is **1:1 with `maid_id`**. The 50 publisher-specific hashes per MAID exist for forward audit / chain-of-custody on the MAID document, but they are not bid-time keys — the bid engine never asks Redis to resolve a publisher-specific hash.

That gives two viable designs for the resolution step:

| Option | Mechanism | Extra storage |
| --- | --- | --- |
| **A. Derive `maid_id` from `identity_token`** | DNA pipeline derives `maid_id` deterministically from the same canonical hash that the bid engine produces. The MAID document is keyed `maid:<identity_token>`, and Redis does the resolution implicitly via the key namespace. No reverse index. | 0 |
| **B. Separate reverse-index keyspace** | Keep `maid_id` opaque and store one STRING per MAID: `identity:<identity_token> -> <maid_id>`. 500 M entries at ~90 B each. | ~45 GB |

**Recommendation: Option A** if the DNA pipeline can guarantee `maid_id` is derived deterministically from the canonical identity hash. That is operationally simpler at scale (no separate index to keep in sync with MAID writes) and removes one Redis round trip from the bid path entirely. **Fall back to Option B** if the existing `maid_id` allocation is opaque or carries other semantics that have to be preserved.

The earlier draft of this spec sized a reverse index at ~2.25 TB by counting all 50 publisher-specific hashes per MAID. That was wrong: those hashes never reach Redis, so they don't need a Redis-side index. The corrected number is ~45 GB under Option B and zero under Option A.

### 1.3 Other keyspaces

| Keyspace | Size estimate | Notes |
| --- | --- | --- |
| `aud:{maid_id}` (precomputed candidate IDs) | 100–300 GB | 500 M MAIDs × ~30–100 candidate IDs × 6 B per ID + key overhead. |
| `fcap:{maid_id}` (per-MAID frequency hash) | 200–500 GB | Sparse: only MAIDs that actually got an impression today carry a hash. |
| `campaign:{id}` + `campaign_state:{id}` + `bm:servable` + `idx:*` | < 1 GB | 5 K ads is negligible at this scale. |
| Operational / metadata keys | < 1 GB |  |

### 1.4 Totals and headroom

| Component | Size |
| --- | --- |
| MAIDs | 7.5 TB |
| Identity → MAID resolution | 0 (Option A) or ~45 GB (Option B); negligible either way |
| `aud:` precompute | ~0.2 TB |
| `fcap:` | ~0.4 TB |
| Ads + indexes + meta | < 1 GB |
| **Logical total** | **~8.1 TB** |
| With one replica per primary (HA) | **~16.2 TB** |
| With 25% headroom for fragmentation, partial updates, taxonomy-feedback rewrite churn | **~20 TB** |

### 1.5 Throughput

Per bid request, the prototype's `hybrid_bitmap_taxonomy` mode performs:

1. `GET identity:<identity_token>` — resolve MAID **(skipped under §1.2 Option A; the maid_id is derived in app memory)**
2. `HMGET maid_hot:<maid_id> ...` — scoring profile
3. Lua bitmap script over `aud:<maid_id>` and `bm:servable` — gated candidate list
4. Pipelined `HGETALL campaign:<id>` for surviving candidates — campaign metadata
5. `HMGET fcap:<maid_id> <campaign_ids>` — frequency counters

That's 3–4 server-side operations per bid under Option A (4–5 under Option B). The pipelined campaign fetch is one network round trip but multiple ops. Plus pacing-state writes from the win-event feedback path (~1 op per win) and frequency-cap increments (~1 op per impression).

| Bid rate | Read ops/s (est., Option A) | Read ops/s (est., Option B) | Write ops/s (est.) |
| --- | --- | --- | --- |
| 100 K bids/s | ~400 K | ~500 K | ~10–20 K |
| 1 M bids/s | ~4 M | ~5 M | ~100–200 K |

**Working assumption: design for ~5 M ops/s steady-state at the 1 M bid/s target** (the higher of the two columns, so the cluster has headroom regardless of which resolution option is chosen).

### 1.6 Shard sizing

There are two viable hardware paths for this dataset. The shard-sizing math differs between them, so each is calculated separately and a recommendation is given in §1.9.

#### 1.6.A All-RAM (Redis Enterprise standard)

Rule-of-thumb is ~25 GB usable memory per shard and ~25 K ops/s per shard before tail-latency degrades.

| Constraint | Calculation | Required shards |
| --- | --- | --- |
| Memory | 20 TB / 25 GB | ~800 |
| Throughput @ 100 K bid/s | 520 K / 25 K | 21 |
| Throughput @ 1 M bid/s | 5.2 M / 25 K | 208 |

Memory dominates. Provision **~800 primary shards with one replica each → 1600 shards total**.

#### 1.6.B Redis Flex (RAM + local NVMe SSD)

Redis Flex keeps a configurable hot working set in RAM and tiers cold keys to local NVMe SSD. The relevant parameters for this workload:

- **RAM ratio:** 5% minimum is a reasonable starting point for a point-lookup workload like this one. Below 5%, working-set churn becomes a real risk and needs to be measured rather than assumed safe.
- **Per-shard capacity:** ~250 GB total (RAM + SSD) is a reasonable target. A Flex shard absorbs roughly 10× the data of an all-RAM shard at the same shard count because the SSD tier carries the cold portion.
- **Per-shard throughput:** lower than RAM-only at the same shard count, because cold-key reads must come from SSD. Local-NVMe reads are roughly an order of magnitude slower than RAM reads at the median, and the spread between median and tail is wider on SSD than on RAM. Plan for ~10–15 K ops/s/shard until the working set has stabilized, then re-measure.

| Constraint | Calculation | Required shards |
| --- | --- | --- |
| Total capacity | 20 TB / 250 GB | ~80 |
| Throughput @ 100 K bid/s | 520 K / 12 K (conservative per-shard) | ~44 |
| Throughput @ 1 M bid/s | 5.2 M / 12 K | ~435 |

At the 1 M bid/s target, throughput crosses the capacity-driven shard count and becomes the binding constraint on Flex. Two options:

1. Raise the shard count to ~450 primaries (~900 with replicas) so each shard sees less load. Per-shard data drops to ~45 GB, well under the 250 GB cap.
2. Keep ~80 capacity-sized shards and accept a higher SSD-read share during peak. Whether this stays inside the 10 ms p99 budget needs to be measured, not assumed.

**Working assumption: option (1).** Provision **~450 primary shards with one replica each → 900 shards total** for Flex. Capacity per shard ends up ~45 GB used out of the 250 GB cap, leaving substantial headroom for taxonomy-feedback growth.

| Mode | Primary shards | Total shards | Avg used per shard | RAM per shard | Cluster RAM | Cluster SSD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| All-RAM | ~800 | ~1600 | ~25 GB | ~25 GB | ~40 TB | 0 |
| Flex (capacity-bound) | ~80 | ~160 | ~250 GB | ~12.5 GB (5%) | ~2 TB | ~38 TB |
| Flex (throughput-bound, 1 M bid/s) | ~450 | ~900 | ~45 GB | ~2.3 GB (5%) | ~2 TB | ~38 TB |

The Flex variants land at roughly the same cluster RAM (~2 TB) regardless of shard count because the 5% ratio is applied per-shard and the total dataset is fixed. Cluster SSD likewise stays around 38 TB.

### 1.7 Node sizing — Option A: All-RAM

| Node shape | Usable RAM/node | Shards/node (CPU-bound) | Nodes for 1600 shards |
| --- | --- | --- | --- |
| `n2-highmem-32` (256 GB) | ~200 GB | ~8 | ~200 |
| `n2-highmem-64` (512 GB) | ~400 GB | ~12–16 | ~110 |
| `n2-highmem-80` (640 GB) | ~500 GB | ~16–20 | ~80 |

**Working assumption: 75 × `n2-highmem-64` with rack-zone awareness across 3 GCP zones (25 nodes per zone).** This gives a reasonable shard density, headroom for proxy CPU on each node, and tolerates a single-zone outage.

### 1.8 Node sizing — Option B: Redis Flex

Flex needs three things on each node: enough RAM for the hot working set per shard, enough local NVMe SSD for the cold tier, and enough vCPU to drive both. GCP families that pair high RAM with attached local SSDs:

| Node shape | vCPU | RAM | Local SSD (max attach) | Notes |
| --- | ---: | ---: | --- | --- |
| `n2-highmem-32` + 8 × 375 GB local SSD | 32 | 256 GB | 3 TB | ~12 shards/node. Mature N2 platform with manual local-SSD attach. |
| `n2-highmem-64` + 16 × 375 GB local SSD | 64 | 512 GB | 6 TB | ~25 shards/node. Default recommendation below. |
| `n2-highmem-80` + 24 × 375 GB local SSD | 80 | 640 GB | 9 TB | ~32 shards/node. Highest density on N2. |
| `c3-standard-176-lssd` | 176 | 704 GB | 12 TB | ~50 shards/node. Newer Sapphire Rapids platform; SSD attach is built into the SKU. |
| `z3-highmem-88` | 88 | 704 GB | 12 TB (NVMe-optimized) | Storage-optimized family designed for this kind of RAM+SSD workload. Worth evaluating if available in the chosen region. |

(Local-SSD limits are GCP per-VM caps at the time of writing — re-verify against the GCP machine-types reference at apply time. Local SSDs on GCP are 375 GB partitions; total local SSD per VM is the partition count × 375 GB. They are NVMe by default on N2 and required-NVMe on C3 / Z3.)

Per-node sizing on `n2-highmem-64` + 6 TB local SSD:

- 25 shards × 12.5 GB RAM = 312 GB used / 512 GB available (covers RAM + Redis Enterprise overhead + page cache)
- 25 shards × 250 GB SSD = 6.25 TB → just over the per-VM SSD cap; size at 24 shards/node to stay under
- 24 shards × 25 K ops/s ≈ 600 K ops/s/node — fine for 100 K bids; at 1 M bids consider splitting to 12 shards/node and doubling node count

**Working assumption for the capacity-sized cluster (80 primaries, 160 shards total): 9 × `n2-highmem-64` + 6 TB local SSD across 3 zones (3 nodes per zone).** That gives ~17–18 shards per node with comfortable RAM and SSD headroom. Add 2 management nodes (small `n2-standard-8`) for the cluster control plane → **11 nodes total**.

For the throughput-sized cluster (450 primaries, 900 shards): scale to **36 × `n2-highmem-64` + 6 TB local SSD** across 3 zones (12 nodes per zone), plus 2 management nodes → **38 nodes total**.

#### 1.8.A Latency consideration for Flex

The bid path is ~3–4 random reads across very large keyspaces (`maid_hot:`, `aud:`, `fcap:`, plus `identity:` if §1.2 Option B is chosen). Each MAID is touched infrequently, so the hot-set hit rate for those reads is **low by construction** — a meaningful fraction of reads will be served from SSD rather than RAM. Local-NVMe reads are roughly an order of magnitude slower than RAM reads at the median, and tail behavior under load is harder to bound. Indicative impact on the bid path:

| Bid step | All-RAM cost | Flex cost (cache miss) |
| --- | --- | --- |
| `GET identity:<identity_token>` (Option B only) | <0.5 ms | ~0.5–2 ms |
| `HMGET maid_hot:<id> ...` | <0.5 ms | ~0.5–2 ms |
| Lua bitmap on `aud:<id>` | <0.5 ms | ~0.5–2 ms |
| pipelined `HGETALL campaign:<id>` (5 K ads, hot in RAM) | <1 ms | <1 ms (will stay in RAM) |
| `HMGET fcap:<id>` | <0.5 ms | ~0.5–2 ms |
| **Total decision-path p99 estimate (Option A)** | **<4 ms** | **2.5–8 ms** |
| **Total decision-path p99 estimate (Option B)** | **<5 ms** | **3–10 ms** |

The Flex p99 estimate brushes against the 10 ms end-to-end SLA, so this is the single most important number to **measure** before committing to Flex for production. Several configuration levers can pull p99 back:

- The bid sampler in any DSP is heavily skewed: a small fraction of MAIDs are "hot" at any time and the rest are long-tail cold. If the RAM ratio is sized for that hot fraction, steady-state reads stay in RAM and only the long tail of cold lookups pays the SSD cost. Whether 5% RAM is enough for a given workload depends on the actual hot-set distribution and should be measured.
- `bm:servable` and the 5K-entry ads cache should be pinned to RAM. Redis Enterprise supports per-key-pattern tier pinning via the auto-tiering policy, or alternatively those keys can live on a small all-RAM database alongside the Flex one.
- The throughput-sized variant (450 primaries) helps because lower per-shard load reduces SSD queue depth.

### 1.9 Hardware option recommendation

| Property | All-RAM | Flex (capacity-sized) | Flex (throughput-sized) |
| --- | --- | --- | --- |
| Total shards | ~1600 | ~160 | ~900 |
| Cluster RAM | ~40 TB | ~2 TB | ~2 TB |
| Cluster SSD | 0 | ~38 TB | ~38 TB |
| Compute nodes | ~75 (`n2-highmem-64`) | ~9 (`n2-highmem-64` + 6 TB lssd) | ~36 (`n2-highmem-64` + 6 TB lssd) |
| Bid p99 vs 10 ms budget | comfortable | **needs measurement** (cold-key SSD reads) | needs measurement, less risk than capacity-sized |
| Operational complexity | lower (single tier) | higher (RAM ratio, working-set warmup) | higher |

**Recommendation for the first scale test: run both All-RAM and Flex (throughput-sized).** That gives a side-by-side comparison of bid-decision latency at the 1 M bid/s target across two clusters with roughly 2× difference in node count (~75 vs ~36) and very different RAM/SSD compositions. If Flex p99 stays under 10 ms with margin, the smaller node count may be worth the operational complexity.

If only one option can be tested, run **All-RAM first** — it's the lower-risk path against the 10 ms SLA, and the latency floor it establishes is the reference number Flex has to come close to.

### 1.10 Load-generator sizing

A single `n2-standard-16` running an `httpx`-async load driver against Redis-backed FastAPI can sustain on the order of 5–10 K requests/s before saturating CPU or local file descriptors.

| Bid rate | Load-gen VMs (est.) |
| --- | --- |
| 100 K bids/s | 12–20 |
| 1 M bids/s | 100–200 |

**Working assumption: a GCP Managed Instance Group of `n2-standard-16` VMs configured with autoscaling between 0 and 250 instances**, scaled by either CPU utilization (target 65%) or by a manual size for each test scenario.

---

## 2. Open Questions To Settle Before Applying

These are the assumptions that drive the topology. Confirm with the deploying team and the Redis SE team before `terraform apply`.

1. **MAID encoding**: JSON STRING vs HASH vs RedisJSON `JSON.SET`. Affects per-MAID footprint and how partial-update writes (single-label taxonomy refreshes) land on the wire.
2. **Identity → MAID resolution**: see §1.2 and open question 9 below. Whether to derive `maid_id` deterministically from the canonical identity hash (no Redis-side reverse index) or keep `maid_id` opaque (~45 GB STRING index) is the most consequential schema decision in the spec.
3. **HA target**: 1 replica per primary is the §1.4 assumption. Is 2 replicas required for cross-zone fault tolerance? That doubles RAM.
4. **Persistence**: AOF + RDB snapshots add disk + memory overhead. Confirm what is required for cold-start recovery at the ~20 TB working footprint.
5. **Rack/zone awareness**: confirm the deployment region and zones. The Terraform module supports rack-zone awareness; default to a 3-zone topology in the chosen region.
6. **Single-call shape**: the workload requirements call for a strong preference toward a single Redis call per bid request. The prototype's `hybrid_bitmap_taxonomy` mode is 4–5 ops via Lua + pipelining. A true single-call shape would compose all five steps into one Lua script or Redis Function; feasible but a separate prototype task. Decide whether the scale test exercises the current 4-op path or a consolidated 1-op path.
7. **Float-threshold filter complexity bound**: the prototype generator caps filters at 4 atoms; production may have more. Settle the bound before generating synthetic data at 500 M scale.
8. **Hardware path — All-RAM vs Redis Flex**: see §1.6–1.9. Flex needs roughly half the node count at the throughput-sized provisioning, or about an eighth at the capacity-sized provisioning, to host the same dataset; the question for the 10 ms p99 budget is whether the SSD-backed read tail on a cold-key-dominated workload stays inside the budget. Recommendation is to measure both side-by-side at the 1 M bid/s scenario before committing. Confirm that local-NVMe-attached node shapes (`n2-highmem-64` with 16 × 375 GB local SSD, or `c3-standard-176-lssd`, or `z3-highmem-88` if available in the chosen region) are acceptable to procurement — local SSDs are ephemeral on GCP, so cluster-wide replication and persistence settings have to compensate.
9. **Identity → MAID resolution**: see §1.2. Choose between (A) deriving `maid_id` deterministically from the canonical identity hash so no Redis-side reverse index is needed, or (B) keeping `maid_id` opaque and storing a 500 M-entry reverse index. (A) saves a Redis round trip on every bid request and eliminates ~45 GB of index storage; (B) preserves whatever semantics the existing `maid_id` allocation carries.

---

## 3. Provisioning

### 3.1 Terraform module

Use [`redis-field-engineering/terraform-gcp-redis-enterprise`](https://github.com/redis-field-engineering/terraform-gcp-redis-enterprise) at a pinned tag. The relevant module variables (verify against the module's `variables.tf` at the chosen tag).

#### 3.1.A Option A — All-RAM cluster

```hcl
module "redis_enterprise" {
  source = "git::https://github.com/redis-field-engineering/terraform-gcp-redis-enterprise.git?ref=<pinned-tag>"

  project_id = var.project_id
  region     = var.gcp_region
  zones      = var.gcp_zones

  cluster_name      = "dsp-bench-ram"
  node_count        = 75
  machine_type      = "n2-highmem-64"
  data_disk_size_gb = 1024
  data_disk_type    = "pd-ssd"

  # No local SSDs for All-RAM — disk is for persistence only.
  local_ssd_count   = 0

  # Networking
  network     = google_compute_network.bench.self_link
  subnetwork  = google_compute_subnetwork.bench.self_link
  allow_cidrs = var.allow_cidrs

  # Redis Enterprise
  re_admin_email    = var.re_admin_email
  re_admin_password = var.re_admin_password
  re_license_key    = var.re_license_key

  labels = {
    purpose = "dsp-bench"
    tier    = "all-ram"
  }
}
```

#### 3.1.B Option B — Redis Flex cluster

```hcl
module "redis_enterprise_flex" {
  source = "git::https://github.com/redis-field-engineering/terraform-gcp-redis-enterprise.git?ref=<pinned-tag>"

  project_id = var.project_id
  region     = var.gcp_region
  zones      = var.gcp_zones

  cluster_name      = "dsp-bench-flex"
  node_count        = 36                    # throughput-sized; 9 for capacity-sized
  machine_type      = "n2-highmem-64"
  data_disk_size_gb = 512                   # only persistence, since SSD tier is local
  data_disk_type    = "pd-ssd"

  # Local NVMe for the Flex tier. n2-highmem-64 supports up to 24 × 375 GB
  # local SSD. 16 × 375 GB = 6 TB per node.
  local_ssd_count   = 16
  local_ssd_interface = "NVME"

  # Mount + format the local SSDs as a single ext4 filesystem under
  # /var/opt/redislabs/flash so Redis Enterprise picks them up as the Flex tier.
  flex_mount_path = "/var/opt/redislabs/flash"

  network     = google_compute_network.bench.self_link
  subnetwork  = google_compute_subnetwork.bench.self_link
  allow_cidrs = var.allow_cidrs

  re_admin_email    = var.re_admin_email
  re_admin_password = var.re_admin_password
  re_license_key    = var.re_license_key

  labels = {
    purpose = "dsp-bench"
    tier    = "flex"
  }
}
```

Verify the exact variable names (`local_ssd_count`, `local_ssd_interface`, `flex_mount_path`) against the module's `variables.tf` at the pinned tag — older module versions may require the local SSDs to be wired in via a startup script or a sidecar `null_resource` that runs `mdadm` and `mkfs.ext4` against `/dev/nvme*` devices before the Redis Enterprise cluster registers them.

Local SSD specifics on GCP:

- 375 GB partitions, NVMe interface required on N2 / C3 / Z3.
- **Ephemeral**: data is lost when the VM is stopped or moved. The cluster has to tolerate node loss via replication; rely on AOF / RDB on the persistent `pd-ssd` for cross-node-loss recovery.
- For capacity-sized Flex (9 nodes), losing one node loses up to ~4 TB of SSD-tier data; the remaining 8 nodes have to absorb that under shard rebalancing. Plan for ~30 min rebalance time per failed node at this density.

Notes (apply to both options):

- Pin to a known-good module tag, not `main`.
- Allowed CIDRs should restrict cluster admin and data-plane traffic to the load-gen VPC.
- Use a dedicated VPC for the test; do not piggyback on a production network.

### 3.2 Cluster configuration via `redisctl`

Once nodes are up, configure the database with `redisctl` rather than the Redis Enterprise UI so the configuration is reproducible and source-controlled. Both options should expose an OSS-cluster-compatible endpoint so the Python `redis` client and the Lua bitmap script work without modification.

#### 3.2.A All-RAM database

```bash
redisctl --cluster dsp-bench-ram database create \
  --name maid_graph \
  --memory-size 20000000000000   `# 20 TB total (matches §1.4 sized footprint)` \
  --shard-count 800 \
  --replication enabled \
  --rack-aware enabled \
  --eviction-policy noeviction \
  --persistence aof-every-1-second
```

#### 3.2.B Redis Flex database

```bash
redisctl --cluster dsp-bench-flex database create \
  --name maid_graph \
  --memory-size 20000000000000   `# 20 TB total dataset` \
  --bigstore enabled              `# enable Flex tier` \
  --bigstore-ram-size 2000000000000  `# 2 TB RAM ≈ 5% (per-shard hot tier)` \
  --shard-count 450               `# throughput-sized; use 80 for capacity-sized` \
  --replication enabled \
  --rack-aware enabled \
  --eviction-policy noeviction \
  --persistence aof-every-1-second

# Pin small, hot keyspaces to RAM so they never get tiered to SSD.
# `bm:servable` (5K-bit bitmap) and the 5K-entry ads cache should always be in RAM.
redisctl --cluster dsp-bench-flex database update maid_graph \
  --bigstore-ram-keys 'bm:*,campaign:*,campaign_state:*,idx:*,meta:*'
```

(Exact `redisctl` flags depend on the installed version — older builds use `--rof` instead of `--bigstore`, and some versions want a separate `database tier set` subcommand for the RAM-pinned key patterns. Treat the above as the intended shape and verify against the version installed by the Terraform module.)

For Flex specifically, two operational settings matter at the database level:

- **RAM ratio enforcement**: Redis Enterprise will warn if a database is configured below the per-shard RAM-ratio policy (default 5%). Do not lower this for the test — the working-set hit-rate analysis in §1.8.A assumes the 5% floor.
- **Auto-tiering policy**: leave at the default LRU-based promotion. The bid path naturally promotes hot MAIDs into the RAM tier as they are read; cold tail-of-distribution MAIDs stay on SSD.

### 3.3 Load generator infrastructure

A separate Terraform module creates:

- A Compute Engine instance template based on a custom image with the prototype repo + `uv`/`pip` dependencies pre-installed.
- A Managed Instance Group (MIG) using that template.
- Autoscaling policy: target 65% CPU, min size 1, max size 250.
- An internal HTTP(S) load balancer in front of the FastAPI service tier. Note: the load generators target the FastAPI service, which then talks to Redis Enterprise. For pure-Redis throughput characterization, an alternative driver that bypasses FastAPI and issues `redis-py` calls directly may also be needed.
- A separate, smaller MIG hosts the FastAPI service tier itself (~20 `n2-standard-16` instances behind an internal L7 LB), so we can isolate "service overhead" from "Redis cost" in the latency breakdown.

### 3.4 Observability

- Each Redis Enterprise node exports its metrics endpoint to the cluster's bundled Prometheus.
- A separate Prometheus + Grafana VM in the bench VPC scrapes the FastAPI service tier (`/metrics`) and the Redis Enterprise cluster.
- Reuse the existing Grafana dashboards in `observability/grafana/` as the starting set; add panels for cluster-side `instantaneous_ops_per_sec`, slowlog rate, and per-shard `used_memory`.

---

## 4. Data Generation And Load

The synthetic generator in `data/synthetic.py` produces 4 K MAIDs by default. For the full-scale test we need a 500 M MAID dataset. Two options:

1. **Scale up the existing generator** to 500 M users by streaming output and writing directly into Redis with pipelined `HSET` / `SET`. The generator is deterministic per seed, so we can shard the work across N writer VMs by partitioning user_id ranges. This is the recommended path for a first run because it reuses the same model as the prototype.
2. **Synthesize a minimal MAID profile** (no `weights`, no `frequency_history`, just the fields the bid path reads) and skip the precompute. This makes the load smaller and faster but does not exercise the precompute code path.

Recommend (1). Estimated load time at ~50K writes/sec from 8 writer VMs: 500 M / 400 K writes/sec ≈ 21 minutes.

Pipeline shape per writer VM:

```text
generate batch of 100K users
build pipeline of:
  - HSET maid:<id> ...
  - SET identity:<identity_token> <maid_id>  (50× per user)
  - SET aud:<maid_id> <json>
  - SETBIT bm:servable <bit> 1  (5K bits, written once globally)
EXEC
```

The 5K ad universe is loaded once from a fixed JSON fixture before the MAID load.

---

## 5. Test Scenarios

Run scenarios in order, tear down load generators between them, keep the cluster up across the sequence. Each scenario writes its own results file under `reports/generated/full_scale/<cluster>/scenario_<n>_<name>.json` where `<cluster>` is `ram` or `flex`.

| # | Scenario | Bid rate | Duration | What it answers |
| --- | --- | --- | --- | --- |
| 0 | Connectivity smoke | 1 RPS | 60 s | Service tier reaches Redis Enterprise; `bm:servable` is populated; identity resolution works. |
| 1 | Warmup | 5 K RPS | 5 min | Fill connection pools, JIT warm Lua scripts. **For Flex**: this is also the SSD-tier warmup phase; the hit-rate against the RAM tier should rise toward steady-state during this window. |
| 2 | Steady-state at the near-term throughput target | 100 K RPS | 30 min | Latency p50/p95/p99 against the 10 ms SLA at the immediate scale. |
| 3 | Steady-state at long-term target | 1 M RPS | 30 min | Latency at the 10× scale. Confirms shard count and CPU headroom. |
| 4 | Soak | 200 K RPS | 6 h | Memory leak detection; AOF rewrite behavior; pacing-update churn under sustained writes. **Flex-specific**: surfaces SSD wear and RAM-tier eviction churn under sustained mixed traffic. |
| 5 | Failure injection — single zone | 100 K RPS | 30 min | Drop one zone's nodes; measure failover and tail latency during recovery. **Flex-specific**: also measures shard rebalance time; with 6 TB local SSD per node, losing a node forces 6 TB of cold-tier data to repopulate from replicas. |
| 6 | Pacing-update storm | 100 K RPS reads + 50 K RPS writes | 15 min | Simulate the win-event feedback path updating `campaign_state:*` at peak. |
| 7 | Cold-key sweep (**Flex only**) | 50 K RPS | 15 min | Drive deliberately cold MAID lookups (random uniform across 500 M, no hot/cold sampler skew) to characterize the SSD-tier tail latency that the §1.8.A analysis flags as the main p99 risk. |

For each scenario, the load generator records the same fields as `loadtest/run.py` plus end-to-end latency, error rate, and Redis-side `instantaneous_ops_per_sec` snapshots every 10 s. On the Flex cluster, also capture per-shard `bigstore_ram_hit_ratio` (target: > 95% steady-state) and `bigstore_io_latency_ms` (target: p99 < 1 ms).

### 5.1 Side-by-side comparison

If both clusters are provisioned, run scenarios 0–4 on each, in this order:

1. All-RAM cluster, scenarios 0–4. Capture results under `reports/generated/full_scale/ram/`.
2. Flex cluster, scenarios 0–4 + 7. Capture results under `reports/generated/full_scale/flex/`.

The deliverable in §9 is a comparison table with p50, p95, p99 decision-path latency for each scenario × cluster cell, plus throughput sustained, plus per-cluster shape (node count, total RAM, total SSD). That collapses the latency-vs-density decision onto a single page.

---

## 6. Pass / Fail Criteria

Per scenario:

- **Latency**: p99 end-to-end < 10 ms (workload requirement). p99 server-side decision-path < 5 ms (the prototype's contribution to the budget).
- **Error rate**: < 0.1%.
- **Throughput**: sustained at the target RPS for the full duration; no `instantaneous_ops_per_sec` collapse > 10%.
- **Memory**: cluster `used_memory` stays within 80% of provisioned across all shards.
- **Quality** (for the offline-eval harness, run separately at the start and end): NDCG@K within ±0.005 of the small-scale baseline. This catches regressions caused by, e.g., Lua-script edge cases at scale.

If a scenario fails any criterion, capture the cluster state with `redisctl debuginfo`, the FastAPI service logs, and the load-generator histograms before proceeding.

---

## 7. Tear-down

```bash
terraform -chdir=terraform/gcp-load-gen destroy
terraform -chdir=terraform/gcp-redis-enterprise destroy
```

Expected total runtime end-to-end: ~12 hours including soak.

---

## 8. Out Of Scope

- **Single-call shape (Lua/Redis-Functions consolidation):** the test exercises the prototype's current 4–5-op shape. A consolidated single-call path is a separate prototype change (see §2.6).
- **Online taxonomy-feedback loop:** no live feedback ingestion. Taxonomy scores are static for the duration of each scenario.
- **Multi-region:** the workload requirements call for a single-region deployment; this spec follows that. Cross-region replication is not tested.
- **Cost / pricing:** out of scope for this spec. Sizing here is in node count, RAM, SSD, and ops/sec. Pricing decisions belong in a separate document once the measured topology is settled.

---

## 9. Deliverables

After the test completes, this repo should contain:

- `reports/generated/full_scale/scenario_<n>_<name>.json` — one file per scenario, same shape as `loadtest/run.py` output plus cluster-side metrics snapshots.
- `reports/full_scale_summary.md` — written from the JSON files, structured the same way as `reports/benchmark_report.md` (retrieval overview table + methodology + per-scenario sections).
- `terraform/gcp-redis-enterprise/` — the pinned cluster module invocation.
- `terraform/gcp-load-gen/` — the load-generator MIG module.
- An updated `README.md` "Native VM Latency" section pointing readers at the full-scale results.
