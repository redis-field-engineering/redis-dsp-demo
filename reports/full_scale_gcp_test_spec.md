# Full-Scale GCP Benchmark — Spec

**Status:** plan only. No infrastructure has been provisioned. Sign off on the sizing numbers below before applying any Terraform.

This spec describes how to validate the prototype against a production-shaped workload: ~500 M MAIDs, ~5 K active ads, AND/OR/NOT taxonomy filters on float scores, target end-to-end bid latency `< 10 ms`, target throughput `100 K → 1 M` bid requests per second.

The plan is to stand up a Redis Enterprise cluster on GCP using the [`redis-field-engineering/terraform-gcp-redis-enterprise`](https://github.com/redis-field-engineering/terraform-gcp-redis-enterprise) Terraform module, configure the database with `redisctl`, drive load from a GCP Managed Instance Group of HTTP load generators, and measure bid-decision latency, ranking quality, and cluster-side throughput against the prototype's `hybrid_bitmap_taxonomy` mode.

---

## 1. Sizing Analysis (Pre-Apply)

These numbers are estimates derived from the workload requirements. They are the basis for how big a cluster to provision. **Re-validate them against measured per-key memory before scaling above ~10% of the target footprint.**

### 1.1 Serving objects and per-keyspace assumptions

There is **one** `maid:{maid_id}` keyspace, but the per-MAID hash size differs depending on which serving mode is deployed. That mode-dependent shape is the single biggest factor in §1.2's per-method totals; the per-MAID audience precompute is a close second.

The Redis-side serving objects are:

| Object | Used by | Contents | Working assumption |
| --- | --- | --- | --- |
| `maid:{maid_id}` (full-mode shape) | `full_realtime`, `maid_*_sinter` | all 11 serving fields: `user_id`, `geo`, `state`, `postal_code`, `device`, `device_type`, `card_tier`, `spend_tier`, `segments_json`, `interests_json`, `impression_count` | **~11 KB / MAID → ~5.12 TB** |
| `maid:{maid_id}` (lean-mode shape) | `precomputed_segment`, `hybrid_*` | scoring subset only: `user_id`, `interests_json`, `impression_count`. Static-targeting fields (geo / state / device / segments / etc.) are not present because static targeting was already evaluated offline by the precompute that produced `aud:{maid_id}` | **~9 KB / MAID → ~4.19 TB** |
| `identity:{identity_token}` | all current benchmarked modes | canonical identity → MAID lookup | **~45 GB total** at 500 M MAIDs |
| `aud:{maid_id}` | precomputed / hybrid modes | precomputed candidate campaign IDs | **~1 TB total** (see §1.1.A) |
| `fcap:{maid_id}` | all modes except pure offline ranking | per-MAID frequency counters | **~50 GB at 100 K bid/s** (see §1.1.B); scales with bid rate |
| `campaign:{id}` | all modes | static ad metadata and taxonomy rule | **~256 MB total** |
| `campaign_state:{id}` | `full_realtime`, `maid_*_sinter`, `precomputed_segment`, `hybrid_precompute_plus_realtime` | pacing / budget / mutable delivery state | **~256 MB total** |
| `idx:*` sets | `maid_*_sinter` | geo / state / device / segment indexes | **~8 MB total** (~150 K total set members across all dimensions; negligible at TB scale) |
| `bm:*` bitmaps | `hybrid_bitmap_*` | active / pacing / budget / servable bitmaps | **~8 MB total** |

#### 1.1.0 Why one `maid:` keyspace, not two

The bid path's hybrid modes only need `user_id`, `interests_json`, and `impression_count` from the MAID. An earlier prototype kept those three fields in a separate `maid_hot:{maid_id}` hash so the bid path could read a smaller key. That split was wasteful at production scale: `interests_json` (~9 KB) was duplicated across both keys, costing ~4 TB of redundant storage at 500 M MAIDs.

The current design keeps **one** `maid:{maid_id}` hash whose field set is chosen at deployment time:

- If the production cluster runs `full_realtime` or any `maid_*_sinter` mode (which need static targeting fields online), the hash carries all 11 fields → **~11 KB / MAID, ~5.12 TB total**.
- If the production cluster runs only the precomputed / hybrid modes, the hash can be slimmed to the 3 scoring fields the bid path actually reads → **~9 KB / MAID, ~4.19 TB total**.

Either way, the bid path's `HMGET maid:<id> user_id interests_json impression_count` is the same — only the underlying key size differs. The benchmark code reads the hot subset via `HMGET` so wire cost on the bid path is mode-independent.

Two additional details that affect every method below:

1. The MAID profile has been slimmed down beyond the original draft. Publisher-specific identity arrays are **not** stored on the serving MAID object, age-bucket metadata is not stored, and frequency history lives in `fcap:{maid_id}` rather than inside the MAID hash.
2. The current benchmark implementation uses a separate `identity:{identity_token}` keyspace for every mode. If a future version derives `maid_id` directly from the canonical identity token, subtract ~45 GB and one Redis read from every method below.

#### 1.1.A Audience precompute fanout

The `aud:{maid_id}` STRING is one Redis value per MAID containing a JSON list of ad IDs that pass that MAID's static targeting (geo / state / device / card tier / segments). Without it, the bid path either evaluates all 5 K ads online (`full_realtime`) or fans out across multiple SINTER probes against the inverted indexes (`maid_*_sinter`). With it, candidate generation collapses to one `GET`.

The size of this keyspace is dominated by the *audience-fanout* assumption — how many ads end up in each MAID's list. The customer's filter cascade in their requirements doc shows ~900 of 5 K ads passing the static filters per MAID (5 000 → 2 800 after card tier → 1 600 after device → ~900 after geo). Storing the full pass-through set costs ~8 KB per MAID and ~4 TB across 500 M MAIDs, which inverts the storage story (more than tripling the audience tier).

The prototype caps each list at 150 ads by static-targeting score; at production scale a slightly higher cap of **200 ads** keeps the highest-bid head of the distribution while leaving the long tail to be regenerated on the next batch. With that cap:

```
audience value bytes  ≈ 200 IDs × 9 B JSON ("c00042," shape) + 2 B brackets ≈ 1 800 B
                     + ~80 B Redis STRING overhead per key
                     + ~22 B for the key itself
                     ≈ 1.9 KB per MAID
total                 ≈ 500 M × 1.9 KB ≈ 950 GB ≈ 1 TB
```

The cap is a tunable knob: lowering to 100 cuts the tier to ~500 GB but increases the chance that all top-N ads are unservable at bid time (because pacing/budget churn happens between batch runs). Raising to 500 inflates the tier to ~2.5 TB. **Working assumption: 200, total ≈ 1 TB.**

Per-ad fanout (the inverse view): with 200 ads/MAID and 5 K ads, each ad is in roughly `(200 / 5 000) × 500 M = 20 M` MAID lists on average — so a single ad-targeting change re-sorts ~20 M `aud:` values. That's the order-of-magnitude write cost of the batch precompute job and is the single biggest argument for keeping the precompute on a periodic batch cadence rather than on every ad-config change.

#### 1.1.B Per-MAID frequency hash

The `fcap:{maid_id}` HASH carries `{campaign_id: count}` for ads each MAID has been served *today*; it resets at midnight UTC. It is the only keyspace that scales with bid rate — every winning bid increments one entry — so it deserves its own per-tier sizing.

Working model: 100 M unique MAIDs see at least one ad per day (a 20 % activity rate against the 500 M base), and each winning bid increments one entry. With ~10 % win rate:

| Bid rate | Wins / day | Avg fcap entries / active MAID | Per-MAID listpack size | `fcap:` total |
| --- | ---: | ---: | ---: | ---: |
| 20 K bid/s | ~173 M | ~1.7 | ~110 B | **~11 GB** |
| 100 K bid/s | ~864 M | ~8.6 | ~225 B | **~23 GB** (round to **~50 GB** for safety) |
| 1 M bid/s | ~8.6 B | ~86 | ~6 KB (tips into hashtable repr) | **~600 GB** |

At ≤ 100 K bid/s the listpack representation keeps each `fcap:` very compact (~18 B per entry incl. listpack framing). At 1 M bid/s the entries-per-MAID exceeds the default `hash-max-listpack-entries=128` threshold and Redis converts to hashtable repr, which roughly triples per-entry overhead. **The 1 M tier needs a deliberate sizing pass — see §1.4.**

`size_estimates.py` uses **50 GB** as the SCALED_UP_KEYSPACES default (matches the 100 K tier with safety headroom). The §1.4 tier table shows the per-tier deltas.

### 1.2 Method-specific logical totals at 500 M MAIDs / 5 K ads

Computed by `data/size_estimates.py` using the §1.1 per-keyspace assumptions. Numbers are at the **100 K bid/s** tier; see §1.4 for how `fcap:` shifts at 20 K and 1 M. The `maid:` column reflects which mode-dependent shape that method needs (per §1.1).

| Method | `maid:` shape | Required keyspaces | Logical total |
| --- | --- | --- | ---: |
| `full_realtime` | full (~11 KB) | `identity:` + `maid:` + `campaign:` + `campaign_state:` + `fcap:` | **~5.22 TB** |
| `maid_bruteforce_sinter` | full (~11 KB) | `identity:` + `maid:` + `campaign:` + `campaign_state:` + `fcap:` + `idx:*` | **~5.22 TB** |
| `maid_tightened_sinter` | full (~11 KB) | `identity:` + `maid:` + `campaign:` + `campaign_state:` + `fcap:` + `idx:*` | **~5.22 TB** |
| `precomputed_segment` | lean (~9 KB) | `identity:` + `maid:` + `aud:` + `campaign:` + `campaign_state:` + `fcap:` | **~5.28 TB** |
| `hybrid_precompute_plus_realtime` | lean (~9 KB) | `identity:` + `maid:` + `aud:` + `campaign:` + `campaign_state:` + `fcap:` | **~5.28 TB** |
| `hybrid_bitmap_gating` | lean (~9 KB) | `identity:` + `maid:` + `aud:` + `campaign:` + `fcap:` + `bm:*` | **~5.28 TB** |
| `hybrid_bitmap_taxonomy` | lean (~9 KB) | `identity:` + `maid:` + `aud:` + `campaign:` + `fcap:` + `bm:*` | **~5.28 TB** |

**Notable: at production scale, the storage difference between methods is small (~60 GB).** The lean-mode `maid:` saving (~1 TB) is roughly offset by the `aud:` precompute (~1 TB), so the discriminator between methods is no longer storage — it is bid-time CPU and Redis round-trip count. The whole spec sizes around the heavier of the two paths (`hybrid_*` at ~5.28 TB) so either method's worth of data fits in the same cluster.

The set-index footprint (`idx:*` ~8 MB) is genuinely negligible at this scale — the SINTER methods do not pay a meaningful storage premium over `full_realtime`.

Production deployments only run **one** mode at a time, so the cluster only stores **one** `maid:` shape in practice. The 5.22 vs 5.28 TB split is the real choice the customer makes: pick the mode, pick the shape, size the cluster.

### 1.3 Headroom target for the production candidate

The rest of this spec sizes the cluster for **`hybrid_bitmap_taxonomy`**, because that is the repo's current production-shaped low-latency path: it uses the lean `maid:` shape, precomputed audience lists, bitmap gating, live `fcap`, and still enforces the per-ad float-score `taxonomy_filter`.

For that method, at the 100 K bid/s tier:

| Component | Size |
| --- | --- |
| Logical total | **~5.28 TB** |
| With one replica per primary (HA) | **~10.6 TB** |
| With 25% headroom for fragmentation, partial updates, and taxonomy-feedback rewrite churn | **~13.2 TB** |

This is the working footprint used by §1.6 shard sizing and §3.2 `redisctl` configuration. The 20 K tier is materially identical (fcap is small enough that the difference is < 1 % of the total). The 1 M tier shifts `fcap:` from ~50 GB to ~600 GB, which adds ~1.4 TB after replicas + headroom — see §1.4.

### 1.4 Bid-rate tier sizing

Most of the working footprint is *bid-rate-independent*: `maid:`, `aud:`, `campaign:`, `identity:`, and the bitmaps don't grow when traffic increases. Only `fcap:` scales with bid rate (per §1.1.B). The table below shows what changes per tier and what doesn't.

| Tier | Bid rate | Read ops/s | Write ops/s | `fcap:` size | Logical total | × 2 replicas, +25 % headroom |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Starter** | 20 K | ~80 K | ~2 K | ~11 GB | ~5.24 TB | **~13.1 TB** |
| **Mid** | 100 K | ~400 K | ~10 – 20 K | ~50 GB | **~5.28 TB** | **~13.2 TB** |
| **Peak** | 1 M | ~4 M | ~100 – 200 K | ~600 GB | ~5.83 TB | **~14.6 TB** |

**Recommended starting point: the 20 K tier.** Capacity-bound on every option (the cluster is sized for the data, not the throughput), so it lets the customer start with something reasonable and scale up by adding shards/nodes — without rebuilding the cluster — once measured load demands it. The 100 K tier reuses the 20 K cluster verbatim. Only the 1 M tier needs a real topology change, and that change is local to Flex (which flips from capacity-bound to throughput-bound at that rate).

Shards / nodes per tier are laid out in §1.6 – §1.9.

### 1.5 Throughput

Per bid request, the prototype's `hybrid_bitmap_taxonomy` mode performs:

1. `GET identity:<identity_token>` — resolve MAID **(skipped if the bid stack derives `maid_id` directly from the canonical identity token in app memory)**
2. `HMGET maid:<maid_id> user_id interests_json impression_count` — scoring subset of the unified `maid:` hash
3. Lua bitmap script over `aud:<maid_id>` and `bm:servable` — gated candidate list
4. Pipelined `HGETALL campaign:<id>` for surviving candidates — campaign metadata
5. `HMGET fcap:<maid_id> <campaign_ids>` — frequency counters

That's 3–4 server-side operations per bid if `maid_id` is derived in app memory (4–5 if the explicit `identity:` lookup is kept). The pipelined campaign fetch is one network round trip but multiple ops. Plus pacing-state writes from the win-event feedback path (~1 op per win) and frequency-cap increments (~1 op per impression).

| Bid rate | Read ops/s (Option A) | Read ops/s (Option B) | Write ops/s | Total ops/s |
| --- | --- | --- | --- | --- |
| 20 K bids/s | ~80 K | ~100 K | ~2–4 K | **~100 K** |
| 100 K bids/s | ~400 K | ~500 K | ~10–20 K | **~520 K** |
| 1 M bids/s | ~4 M | ~5 M | ~100–200 K | **~5.2 M** |

**Working assumption: size for ~5.2 M ops/s steady-state at the 1 M target** (the higher of the two read columns, so the cluster has headroom regardless of resolution option). 20 K and 100 K are both well under any reasonable per-shard throughput limit.

### 1.6 Shard sizing

There are two viable hardware paths for this dataset. The shard-sizing math differs between them, so each is calculated separately and a recommendation is given in §1.9.

#### 1.6.A All-RAM (Redis Enterprise standard)

Rule-of-thumb is ~25 GB usable memory per shard and ~25 K ops/s per shard before tail-latency degrades.

| Constraint | Calculation | Required shards |
| --- | --- | --- |
| Memory @ 20 K & 100 K tiers | 13.2 TB / 25 GB | ~528 |
| Memory @ 1 M tier | 14.6 TB / 25 GB | ~584 |
| Throughput @ 20 K bid/s | 100 K / 25 K | 4 |
| Throughput @ 100 K bid/s | 520 K / 25 K | 21 |
| Throughput @ 1 M bid/s | 5.2 M / 25 K | 208 |

**Memory dominates at every tier.** Throughput is well under capacity until 1 M bid/s, and even there it doesn't bind. The same shard count carries 20 K, 100 K, and 1 M:

- **20 K & 100 K tiers**: ~530 primary shards / **1060 with replicas**
- **1 M tier**: ~585 primary shards / **1170 with replicas** (only the larger `fcap:` at peak rate pushes shard count up by ~10 %)

#### 1.6.B Redis Flex (RAM + local NVMe SSD)

Redis Flex v2 keeps a configurable hot working set in RAM and tiers cold keys + values to local NVMe SSD. Configuration parameters per the published Flex v2 sizing guidance:

- **Per-shard capacity:** **50 GB** total (RAM + SSD) — the standard Flex shard increment.
- **RAM ratio:** **10 % minimum**; recommended starting point is **20 %**, with 30 % – 50 % available for workloads that need more RAM-backed throughput. The ratio is online-tunable.
- **Per-shard throughput** (sustained, published per-RAM-ratio numbers):

  | RAM ratio | Per-shard ops/s | Per-shard RAM | Per-shard SSD |
  | --- | ---: | ---: | ---: |
  | 10 % | ~5 K | 5 GB | 45 GB |
  | 20 % (recommended start) | ~10 K | 10 GB | 40 GB |
  | 30 % | ~15 K | 15 GB | 35 GB |
  | 40 % | ~20 K | 20 GB | 30 GB |
  | 50 % | ~25 K | 25 GB | 25 GB |

  Higher RAM ratios trade SSD capacity for throughput. At 50 % RAM the per-shard throughput is comparable to an All-RAM shard of the same size; at 10 % the throughput halves but the per-shard SSD goes from 25 GB to 45 GB.

- **Latency target:** Flex's published positioning is **p99 < 10 ms acceptable** — same budget as the customer's bid SLA. That alignment is real but tight; whether *this particular workload* (cold-key-dominated random lookups across 500 M MAIDs) sits inside that envelope is the central measurement question for §5.

Sized at the **20 % RAM** recommended starting point (10 K ops/s/shard):

| Constraint | Calculation | Required shards |
| --- | --- | --- |
| Total capacity @ 20 K & 100 K | 13.2 TB / 50 GB | ~264 |
| Total capacity @ 1 M | 14.6 TB / 50 GB | ~292 |
| Throughput @ 20 K bid/s | 100 K / 10 K | ~10 |
| Throughput @ 100 K bid/s | 520 K / 10 K | ~52 |
| Throughput @ 1 M bid/s | 5.2 M / 10 K | ~520 |

**Capacity binds at 20 K and 100 K; throughput binds at 1 M.** At 20 K and 100 K, throughput is well under the 264-shard capacity floor. At 1 M, the 520-shard throughput requirement exceeds the capacity floor, so the cluster has to grow — or the RAM ratio gets bumped up so per-shard throughput rises.

| Tier | Primary shards | Why |
| --- | ---: | --- |
| 20 K bid/s | ~270 (capacity-bound) | sized for the 13.2 TB working footprint |
| 100 K bid/s | ~270 (capacity-bound) | same cluster as 20 K |
| 1 M bid/s | ~520 at 20 % RAM, ~350 at 30 % RAM, ~265 at 40 % RAM (throughput-bound) | trade more cluster RAM for fewer shards |

**Working assumption: 270 primaries / 540 shards total at 20 % RAM for the 20 K – 100 K tier; scale to 520 primaries / 1040 shards at 20 % RAM for the 1 M tier.** A future tuning pass can revisit the 1 M tier at 30 % – 40 % RAM if the customer prefers a smaller shard count and a higher RAM-per-shard footprint.

Per-tier physical footprint side-by-side (Flex sized at 20 % RAM = 10 GB RAM / 40 GB SSD per shard):

| Mode | Primary shards | Total shards | Cluster RAM | Cluster SSD |
| --- | ---: | ---: | ---: | ---: |
| All-RAM (20 K – 100 K) | ~530 | ~1060 | ~26.5 TB | 0 |
| All-RAM (1 M) | ~585 | ~1170 | ~29.3 TB | 0 |
| Flex (20 K – 100 K, 20 % RAM) | ~270 | ~540 | ~5.4 TB | ~21.6 TB |
| Flex (1 M, 20 % RAM, throughput-bound) | ~520 | ~1040 | ~10.4 TB | ~41.6 TB |
| Flex (1 M, 30 % RAM, throughput-bound) | ~350 | ~700 | ~10.5 TB | ~24.5 TB |
| Flex (1 M, 40 % RAM, capacity-bound) | ~292 | ~584 | ~11.7 TB | ~17.5 TB |

Note: at the 1 M tier, raising the RAM ratio from 20 % → 30 % → 40 % keeps cluster RAM roughly flat (~10 – 12 TB) while shrinking cluster SSD, because the higher-throughput RAM ratio reduces the shard count needed. **The 20 % – 40 % RAM range is the tunable knob for trading SSD capacity vs shard count at the 1 M tier.**

The 20 K and 100 K tiers share a cluster shape — same shard count, same node count, same RAM/SSD totals. Only 1 M needs a topology bump.

### 1.7 Node sizing — Option A: All-RAM

The All-RAM cluster needs to host **~26.5 TB of cluster RAM at the 20 K – 100 K tier** (1060 shards × ~25 GB), or **~29 TB at 1 M** (1170 shards × ~25 GB). Each node needs enough vCPU to drive ~25 K ops/s/shard without tail-latency degradation. Larger node shapes give fewer total nodes but a larger blast radius per failure. GCP options across the relevant memory range:

| Node shape | vCPU | RAM | Usable RAM (~90%) | Shards/node (limit) | Nodes @ 20 K–100 K (1060 shards) | Nodes @ 1 M (1170 shards) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `n2-highmem-64` | 64 | 512 GB | ~460 GB | ~18 (RAM-bound) | ~59 | ~65 |
| `n2-highmem-128` | 128 | 864 GB | ~780 GB | ~30 (RAM-bound) | ~36 | ~39 |
| `m3-megamem-128` | 128 | 1.95 TB | ~1.7 TB | ~64 (CPU-bound at ~1 vCPU/shard) | ~17 | ~19 |
| `m1-ultramem-160` | 160 | 3.75 TB | ~3.4 TB | ~80 (CPU-bound) | ~14 | ~15 |
| `m3-ultramem-128` | 128 | 3.9 TB | ~3.5 TB | ~64 (CPU-bound) | ~17 | ~19 |
| `m2-ultramem-208` | 208 | 5.75 TB | ~5.2 TB | ~140 (CPU-bound at ~1.5 vCPU/shard) | ~8 | ~9 |
| `m2-ultramem-416` | 416 | 11.5 TB | ~10.4 TB | ~280 (CPU-bound) | ~4 | ~5 |

(All capacities are GCP per-VM specs at the time of writing — re-verify against the GCP machine-types reference at apply time. The "Shards/node" column applies the lower of the CPU-driven and RAM-driven caps. CPU per shard is sized at ~1.5 vCPU to leave headroom for cluster proxy, replication, and metrics overhead.)

#### 1.7.A Blast radius vs node count

Bigger nodes mean fewer total nodes but more shards (and more RAM) lost when any one fails. Numbers below are at the 20 K – 100 K cluster size:

| Node shape | Cluster size | Shards lost per node failure | RAM lost per node failure | Single-node loss as % of cluster |
| --- | --- | ---: | ---: | ---: |
| `n2-highmem-64` | ~59 nodes | ~18 | ~450 GB | ~1.7% |
| `m1-ultramem-160` | ~14 nodes | ~80 | ~2.0 TB | ~7.1% |
| `m2-ultramem-208` | ~8 nodes | ~140 | ~3.5 TB | ~12.5% |
| `m2-ultramem-416` | ~4 nodes | ~280 | ~7.0 TB | ~25% |

The 4-node `m2-ultramem-416` configuration is too aggressive: 25 % capacity loss on a single-node failure is hard to absorb without violating per-shard p99 SLOs during the rebalance window. The 8-node `m2-ultramem-208` configuration is similarly concentrated. A mid-sized memory shape is a better balance here.

**Working assumption: 18 × `m3-megamem-128` across 3 GCP zones (6 nodes per zone)** — sized for the 1 M tier so the same cluster carries 20 K, 100 K, and 1 M without re-provisioning. This gives:

- ~65 shards per node (1170 total capacity vs 1170 needed at 1 M).
- 6 nodes per zone → reasonable zone-failure distribution across three zones.
- Single-node loss is ~5.6% of cluster capacity, materially easier to absorb than the 8-node ultramem layout.

**Smaller starter (20 K–100 K only): 17 × `m3-megamem-128`** if the customer wants to right-size for the immediate target and add a node later when graduating to 1 M.

**Fallback: 15 × `m1-ultramem-160` across 3 zones (5 nodes per zone)** if the team prefers fewer, larger nodes. That puts each node failure at ~7 % of cluster capacity while keeping the cluster size operationally manageable.

**Conservative fallback: 36 × `n2-highmem-128`** for an even smaller blast radius and more conventional fleet shape, at the cost of materially more nodes.

### 1.8 Node sizing — Option B: Redis Flex

Flex needs three things on each node: enough RAM for the hot working set per shard, enough local NVMe SSD for the cold tier, and enough vCPU to drive both. GCP families that pair high RAM with attached local SSDs:

| Node shape | vCPU | RAM | Local SSD (max attach) | Notes |
| --- | ---: | ---: | --- | --- |
| `n2-highmem-32` + 8 × 375 GB local SSD | 32 | 256 GB | 3 TB | Up to ~60 standard 50 GB Flex shards by SSD capacity. |
| `n2-highmem-64` + 16 × 375 GB local SSD | 64 | 512 GB | 6 TB | Up to ~120 standard 50 GB Flex shards by SSD capacity. Default recommendation below. |
| `n2-highmem-80` + 24 × 375 GB local SSD | 80 | 640 GB | 9 TB | Up to ~180 standard 50 GB Flex shards by SSD capacity. Highest density on N2. |
| `c3-standard-176-lssd` | 176 | 704 GB | 12 TB | Up to ~240 standard 50 GB Flex shards by SSD capacity. SSD attach is built into the SKU. |
| `z3-highmem-88` | 88 | 704 GB | 12 TB (NVMe-optimized) | Up to ~240 standard 50 GB Flex shards by SSD capacity. Worth evaluating if available in the chosen region. |

(Local-SSD limits are GCP per-VM caps at the time of writing — re-verify against the GCP machine-types reference at apply time. Local SSDs on GCP are 375 GB partitions; total local SSD per VM is the partition count × 375 GB. They are NVMe by default on N2 and required-NVMe on C3 / Z3.)

Per-node sizing on `n2-highmem-64` + 6 TB local SSD with 50 GB Flex shards at **20 % RAM** (10 GB RAM + 40 GB SSD per shard):

- 24 shards × 10 GB RAM = 240 GB used / 512 GB available (covers the hot tier and leaves substantial headroom for Redis Enterprise overhead, proxy, and page cache)
- 24 shards × 40 GB SSD = 960 GB used / 6 TB available
- 40 shards × 10 GB RAM = 400 GB used / 512 GB available
- 40 shards × 40 GB SSD = 1.6 TB used / 6 TB available

40 shards/node is comfortable; 24 shards/node is roomy. **At 20 % RAM, vCPU is the binding constraint on `n2-highmem-64`** (64 vCPU at ~1.5 vCPU/shard ≈ 40 shards). Higher RAM ratios push RAM per shard higher (e.g., 30 % → 15 GB / shard, so 30 shards/node hits 450 GB RAM); at 40 % RAM RAM-per-node becomes the binding constraint (`n2-highmem-128` becomes more attractive for higher RAM ratios).

**Working assumption for the capacity-sized cluster (270 primaries, 540 shards total — covers both the 20 K and 100 K tiers at 20 % RAM): 14 × `n2-highmem-64` + 6 TB local SSD across 3 zones (4–5 nodes per zone).** That gives ~39 shards per node (~1.55 TB SSD used out of 6 TB available, ~390 GB RAM used out of 512 GB available). Add 2 management nodes (small `n2-standard-8`) for the cluster control plane → **16 nodes total**.

For the throughput-sized cluster (520 primaries, 1040 shards at 20 % RAM — required only at the 1 M tier): scale to **30 × `n2-highmem-64` + 6 TB local SSD** across 3 zones (10 nodes per zone) at ~35 shards/node, plus 2 management nodes → **32 nodes total**.

The 1 M tier is roughly 2× the cluster of the 20 K – 100 K tier at 20 % RAM. Bumping the 1 M tier to 30 % RAM (~350 primaries, 700 total shards) can collapse the cluster back toward ~20 nodes, at the cost of more RAM per node — `n2-highmem-128` becomes the better fit at that RAM ratio.

#### 1.8.A Latency consideration for Flex

The bid path is ~3–4 random reads across very large keyspaces (`maid:`, `aud:`, `fcap:`, plus `identity:` if the explicit lookup is retained). Each MAID is touched infrequently, so the hot-set hit rate for those reads is **low by construction** — a meaningful fraction of reads will be served from SSD rather than RAM. Local-NVMe reads are roughly an order of magnitude slower than RAM reads at the median, and tail behavior under load is harder to bound. Indicative impact on the bid path:

| Bid step | All-RAM cost | Flex cost (cache miss) |
| --- | --- | --- |
| `GET identity:<identity_token>` (Option B only) | <0.5 ms | ~0.5–2 ms |
| `HMGET maid:<id> user_id interests_json impression_count` | <0.5 ms | ~0.5–2 ms |
| Lua bitmap on `aud:<id>` | <0.5 ms | ~0.5–2 ms |
| pipelined `HGETALL campaign:<id>` (5 K ads, hot in RAM) | <1 ms | <1 ms (will stay in RAM) |
| `HMGET fcap:<id>` | <0.5 ms | ~0.5–2 ms |
| **Total decision-path p99 estimate (Option A)** | **<4 ms** | **2.5–8 ms** |
| **Total decision-path p99 estimate (Option B)** | **<5 ms** | **3–10 ms** |

The Flex p99 estimate brushes against the 10 ms end-to-end SLA, so this is the single most important number to **measure** before committing to Flex for production. Note that "p99 < 10 ms acceptable" is itself the published Flex v2 positioning, so the latency target is achievable by design — the question is whether *this* workload (cold-key-dominated random lookups across 500 M MAIDs with low hot-set hit rate) stays inside that envelope. Several configuration levers can pull p99 back:

- The bid sampler in any DSP is heavily skewed: a small fraction of MAIDs are "hot" at any time and the rest are long-tail cold. The Flex v2 sizing guidance is to **start at 20 % RAM and adjust based on measured latency**. If the working-set hit rate against the RAM tier is too low, raising the RAM ratio to 30 % – 50 % moves more keys back into RAM (and increases per-shard throughput along with it).
- `bm:servable` and the 5K-entry ads cache should be pinned to RAM. Redis Enterprise supports per-key-pattern tier pinning via the auto-tiering policy, or alternatively those keys can live on a small all-RAM database alongside the Flex one.
- The throughput-bound 1 M variant (~520 primaries) helps because lower per-shard load reduces SSD queue depth.
- If 30 % – 40 % RAM is needed to hit p99, expect a corresponding increase in cluster RAM (see the Flex per-RAM-ratio table in §1.6.B).

### 1.9 Hardware option recommendation

Per-tier physical footprint side-by-side:

Flex numbers below assume the recommended **20 % RAM ratio**. Higher ratios change cluster RAM/SSD totals — see the §1.6.B table.

| Tier | Property | All-RAM | Flex (capacity-bound, 20 % RAM) | Flex (throughput-bound, 20 % RAM) |
| --- | --- | --- | --- | --- |
| **Starter (20 K bid/s)** | Total shards | ~1060 | ~540 | n/a |
| | Cluster RAM | ~26.5 TB | ~5.4 TB | n/a |
| | Cluster SSD | 0 | ~21.6 TB | n/a |
| | Compute nodes | ~17–18 (`m3-megamem-128`) | ~14 (`n2-highmem-64` + 6 TB lssd) | n/a |
| **Mid (100 K bid/s)** | Total shards | ~1060 (same cluster as 20 K) | ~540 (same cluster as 20 K) | n/a |
| | Cluster RAM | ~26.5 TB | ~5.4 TB | n/a |
| | Cluster SSD | 0 | ~21.6 TB | n/a |
| | Compute nodes | ~17–18 | ~14 | n/a |
| **Peak (1 M bid/s)** | Total shards | ~1170 | n/a (throughput exceeds capacity floor) | ~1040 |
| | Cluster RAM | ~29.3 TB | n/a | ~10.4 TB |
| | Cluster SSD | 0 | n/a | ~41.6 TB |
| | Compute nodes | ~18–19 | n/a | ~30 (`n2-highmem-64` + 6 TB lssd) |
| **All tiers** | Single-node blast radius | ~6 % of cluster | ~7 % of cluster | ~3 % of cluster |
| | Bid p99 vs 10 ms budget | comfortable | **needs measurement** (cold-key SSD reads) | needs measurement, less risk than capacity-sized |
| | Operational complexity | lower (single tier) | higher (RAM ratio, working-set warmup) | higher |

**Recommendation: start at the 20 K tier on whichever option the customer prefers.** The cluster carries 100 K without modification and only needs scale-up to reach 1 M.

For the first scale test: run **both All-RAM and Flex (throughput-sized)** at 1 M to measure the SSD-tail risk on Flex against the All-RAM reference. If only one option can be tested, run **All-RAM first** — it's the lower-risk path against the 10 ms SLA, and the latency floor it establishes is the reference number Flex has to come close to.

### 1.10 Load-generator sizing

A single `n2-standard-16` running an `httpx`-async load driver against Redis-backed FastAPI can sustain on the order of 5–10 K requests/s before saturating CPU or local file descriptors.

| Bid rate | Load-gen VMs (est.) |
| --- | --- |
| 20 K bids/s | 3–5 |
| 100 K bids/s | 12–20 |
| 1 M bids/s | 100–200 |

**Working assumption: a GCP Managed Instance Group of `n2-standard-16` VMs configured with autoscaling between 0 and 250 instances**, scaled by either CPU utilization (target 65%) or by a manual size for each test scenario.

---

## 2. Open Questions To Settle Before Applying

These are the assumptions that drive the topology. Confirm with the deploying team and the Redis SE team before `terraform apply`.

1. **MAID encoding**: JSON STRING vs HASH vs RedisJSON `JSON.SET`. Affects per-MAID footprint and how partial-update writes (single-label taxonomy refreshes) land on the wire.
2. **Identity → MAID resolution**: see §1.1 and open question 9 below. Whether to derive `maid_id` deterministically from the canonical identity hash (no Redis-side reverse index) or keep `maid_id` opaque (~45 GB STRING index) is the most consequential schema decision in the spec.
3. **HA target**: 1 replica per primary is the §1.3 assumption. Is 2 replicas required for cross-zone fault tolerance? That doubles RAM.
4. **Persistence**: AOF + RDB snapshots add disk + memory overhead. Confirm what is required for cold-start recovery at the ~13.2 TB working footprint for the `hybrid_bitmap_taxonomy` path (~14.6 TB at the 1 M tier).
5. **Rack/zone awareness**: confirm the deployment region and zones. The Terraform module supports rack-zone awareness; default to a 3-zone topology in the chosen region.
6. **Single-call shape**: the workload requirements call for a strong preference toward a single Redis call per bid request. The prototype's `hybrid_bitmap_taxonomy` mode is 4–5 ops via Lua + pipelining. A true single-call shape would compose all five steps into one Lua script or Redis Function; feasible but a separate prototype task. Decide whether the scale test exercises the current 4-op path or a consolidated 1-op path.
7. **Float-threshold filter complexity bound**: the prototype generator caps filters at 4 atoms; production may have more. Settle the bound before generating synthetic data at 500 M scale.
8. **Hardware path — All-RAM vs Redis Flex**: see §1.6–1.9. With the corrected `aud:` precompute footprint, both options are capacity-bound at 20 K and 100 K bid/s, and only Flex flips to throughput-bound at 1 M. At the 20 K – 100 K shared cluster: All-RAM is ~17–18 m3-megamem-128 nodes; Flex (20 % RAM) is ~14 n2-highmem-64+SSD nodes. At 1 M: All-RAM grows by ~10 %; Flex at 20 % RAM roughly doubles to ~30 nodes, while bumping the 1 M Flex tier to 30 % RAM cuts the cluster back toward ~20 nodes at the cost of more RAM-per-node. The meaningful distinction is **bid p99 risk** (RAM-only is comfortable inside the 10 ms budget; Flex's 10 ms p99 target is achievable by design but needs measurement against this workload's cold-key access pattern) versus node count and RAM ratio. Recommendation is still to measure both side-by-side at the 1 M bid/s scenario before committing. Confirm that the chosen Redis Enterprise node shapes (high-memory M3/M1 for All-RAM; local-NVMe-attached `n2-highmem-64`, `c3-standard-176-lssd`, or `z3-highmem-88` for Flex if available in the chosen region) are acceptable to procurement — local SSDs are ephemeral on GCP, so cluster-wide replication and persistence settings have to compensate.
9. **Identity → MAID resolution**: see §1.1. Choose between (A) deriving `maid_id` deterministically from the canonical identity hash so no Redis-side reverse index is needed, or (B) keeping `maid_id` opaque and storing a 500 M-entry reverse index. (A) saves a Redis round trip on every bid request and eliminates ~45 GB of index storage; (B) preserves whatever semantics the existing `maid_id` allocation carries.

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
  # 18 nodes covers both the 20 K – 100 K starter cluster (1060 shards) and
  # the 1 M tier (1170 shards) with capacity to spare. Drop to 17 if you
  # plan to add a node when graduating to 1 M.
  node_count        = 18
  machine_type      = "m3-megamem-128"
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
  # 14 nodes is the capacity-sized cluster that carries the 20 K and 100 K
  # tiers (540 shards). Bump to 30 for the 1 M tier (1040 shards).
  node_count        = 14                    # 30 for the 1 M tier
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
- For capacity-sized Flex (14 nodes), losing one node still loses a meaningful fraction of the SSD tier; that configuration is the 20 K – 100 K floor, not the 1 M topology.

Notes (apply to both options):

- Pin to a known-good module tag, not `main`.
- Allowed CIDRs should restrict cluster admin and data-plane traffic to the load-gen VPC.
- Use a dedicated VPC for the test; do not piggyback on a production network.

### 3.2 Cluster configuration via `redisctl`

Once nodes are up, configure the database with `redisctl` rather than the Redis Enterprise UI so the configuration is reproducible and source-controlled. Both options should expose an OSS-cluster-compatible endpoint so the Python `redis` client and the Lua bitmap script work without modification.

#### 3.2.A All-RAM database

```bash
# Sized to carry 20 K – 100 K bid/s on day one and 1 M bid/s after scaling
# the cluster to 18 nodes / 1170 shards. memory-size at 14.6 TB matches the
# 1 M-tier footprint (§1.4) so resharding doesn't require a memory bump.
redisctl --cluster dsp-bench-ram database create \
  --name maid_graph \
  --memory-size 14600000000000   `# 14.6 TB; matches §1.4 1 M-tier footprint` \
  --shard-count 530               `# 530 covers 20 K – 100 K; reshard to ~585 for 1 M` \
  --replication enabled \
  --rack-aware enabled \
  --eviction-policy noeviction \
  --persistence aof-every-1-second
```

#### 3.2.B Redis Flex database

```bash
redisctl --cluster dsp-bench-flex database create \
  --name maid_graph \
  --memory-size 14600000000000   `# 14.6 TB total (1 M-tier headroom)` \
  --bigstore enabled              `# enable Flex v2 tier` \
  --bigstore-ram-size 2920000000000  `# ~2.92 TB RAM = 20 % of 14.6 TB (Flex v2 recommended start; min 10 %)` \
  --shard-count 270               `# 270 covers 20 K – 100 K; reshard to ~520 for 1 M at 20 % RAM, ~350 at 30 % RAM` \
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

- **RAM ratio enforcement**: Flex v2 enforces a 10 % minimum RAM ratio. The published guidance is to **start at 20 %** and adjust based on measured latency. The working-set hit-rate analysis in §1.8.A assumes the 20 % starting point; raising to 30 % – 50 % is the lever for hitting tighter p99 budgets at higher RAM cost.
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
  - (no separate maid_hot: hash any more — bid path reads scoring fields straight from maid: via HMGET)
  - SET identity:<identity_token> <maid_id>  (1× per user in the current benchmark implementation)
  - SET aud:<maid_id> <json>
  - HSET fcap:<maid_id> ...   (only for users with non-empty counters)
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
