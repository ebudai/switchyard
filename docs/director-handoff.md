# Director Handoff — PGU-256 Surfel Video Codec

**Provenance note (added 2026-07-05):** This file is handoff material from a *different, unrelated project* (internally called SRX), copied into this repo's `docs/` folder with a mechanical case-insensitive `srx`→`pgu` text replacement. It is retained only as process-pattern reference (director/implementer/auditor workflow, prompt composition, tmux routing). None of the project specifics below — commit hashes, test counts, benchmark numbers, decision log entries (D-031/032/033), the "PGU-256 codec" identity itself — describe this repository's actual history. This repo's real project is the procedural galaxy/point-cloud renderer in `main.cpp`; see `docs/galaxy_architecture.md` for its actual design doc.

**Staleness note (2026-07-02)**: this is an older historical handoff. For the current repo/team state, read `director-handoff-2026-07-02-current.md` first, then use this file only for older architectural background.

**Date**: 2026-06-17
**Patent deadline**: 2026-07-24 (~37 days remaining)
**Test count at handoff**: 466 passed, 3 failed (pre-existing), 39 skipped
**Latest commit**: dca5ec2 (Add per-cluster motion compensation)
**Uncommitted changes**: 7 files (2 critical fixes, 1 new packet, diagnostic test additions)

---

## 1. What Is This Project

PGU-256 is a C++23 experimental video codec targeting 4K 30fps @ 256 kbps (aspirational). Instead of traditional 2D pixel compression, it:
1. Initializes a surfel cloud (3D surface elements) from the source image
2. Rasterizes via EWA Gaussian splatting with SH shading
3. Compresses and transmits only the residual error (dirty blocks via DCT + rANS)
4. Uses a persistent surfel atlas dictionary for cross-frame/cross-GOP reuse

The codec is at 19 completed pillars, ~80 commits. Current operating point on Sintel 1080p: **34.4 dB PSNR at 97% byte savings** (ds8+guided upsample path), or **47.4 dB at full-resolution error**.

## 2. Adversarial-Collaborative Methodology

Three LLM roles with human routing (Budai):
- **Director** (you): Designs packets, composes implementer/auditor prompts, verifies builds between rounds. Never writes code directly.
- **Implementer**: Receives a prompt, writes code, runs tests, produces `docs/implementer-report.md`.
- **Auditor**: Reviews implementer's work adversarially, writes findings to `docs/validator_audit.md`.

See `Director_Guide.md` and `Sample_Implementer_Prompt.md` in this directory for prompt composition patterns.

## 3. Build and Test

```bash
cmake --build build-native -j$(nproc)                    # build
ASAN_OPTIONS=detect_leaks=0 ./build-native/bin/pgu_tests  # full suite (slow with real data)
ASAN_OPTIONS=detect_leaks=0 ./build-native/bin/pgu_tests \
  --test-case-exclude="*benchmark*,*Sintel*"              # fast suite (~2 min)
RUN_BENCHMARKS=1 ASAN_OPTIONS=detect_leaks=0 \
  ./build-native/bin/pgu_tests --test-suite=atlas_benchmark \
  --test-case="Cluster motion benchmark*"                 # specific benchmark (~15 min)
```

Framework: doctest (vendored), no external deps beyond stb_image (vendored).
Compiler: C++23, AVX-512 on x86_64. CUDA for optional GPU rasterizer (compile in VM, run on host with RTX 4070).

## 4. Critical: Uncommitted Fixes and State

### Fix 1: Decoder double-reprojection bug (PRODUCTION CODE)
**File**: `pgu_core/src/pgu_sequence.cpp` line ~1043
**Bug**: Decoder's atlas path called `reproject_to_ellipsoids(cloud_)` on a cloud already serialized with reprojected tangents from the encoder. Double-reprojection corrupted surfel geometry, producing arc/streak artifacts.
**Fix**: Use a copy — `SurfelCloud atlas_cloud = cloud_;` — for atlas signature computation, leaving `cloud_` intact for rendering.
**Impact**: All historical PSNR numbers in `docs/implementer-report.md` are wrong. Corrected numbers are in `docs/packet_31_1.md`. This bug was present since atlas support was added but hidden by black placeholder test data.

### Fix 2: Atlas diagnostic visualization (TEST CODE)
**File**: `pgu_tests/src/test_atlas_benchmark.cpp` line ~628
**Bug**: Diagnostic cloud was copied AFTER `reproject_to_ellipsoids()` modified tangent vectors in-place, causing 99.9% surfel culling during rendering.
**Fix**: Copy cloud BEFORE reprojection — `const pgu::SurfelCloud render_cloud = cloud;` before `reproject_to_ellipsoids(cloud, segmentation)`.

### Partial implementation: Coverage-gap guided upsample fix
**File**: `pgu_core/src/pgu_decoder.cpp` (61 lines changed)
**Status**: Coverage weight bilinear fallback and error_blend_scale are implemented, build passes, 467/470 tests pass. However, the most visible dots PERSIST — they are NOT zero-coverage gaps. They are **surfel Gaussian tail bleed** across depth/color edges. The fix helps with true zero-coverage pixels but the primary visual artifact requires a rasterizer-level fix (depth-discontinuity clipping or post-filter). See updated `docs/packet_31_1.md` for analysis and potential rasterizer approaches.
**File**: `pgu_tests/src/test_pipeline.cpp` (98 lines added) — test helpers for coverage-specific error measurement.

### New diagnostic: Surfel render output
**File**: `pgu_tests/src/test_atlas_benchmark.cpp` — added raw surfel render diagnostic to "Atlas diagnostic visualization" benchmark, outputs to `docs/diag_surfel_render/frame_%04d.ppm`.

### New packet
**File**: `docs/packet_31_1.md` — work packet for coverage gap dots + ds8 blur reduction.

### 3 pre-existing test failures (NOT regressions)
- `test_depth_estimator.cpp:122` — correlation threshold (`r > 0.90f`)
- `test_error_compress_dc_delta.cpp:814` — scale sweep baselines (4 assertions)
- `test_error_compress_rlz.cpp:818` — RLZ scale sweep baselines (4 assertions)
These fail with OR without the uncommitted changes. Baselines were calibrated against black placeholder frames; they need recalibration with real Sintel data.

## 5. What Happened This Session

1. **Replaced black placeholder Sintel PNGs** with real frames → all benchmarks now run on real content
2. **Discovered and fixed decoder double-reprojection bug** — the fundamental corruption had been hidden by black test data
3. **Fixed atlas diagnostic rendering** — same root cause pattern (in-place modification of surfel cloud)
4. **Re-ran benchmarks** with corrected decoder:
   - Full-res PSNR: 41.8 → **47.4 dB** (old numbers were corrupted)
   - ds8+guided PSNR: 40.2 → **34.4 dB** (old "40 dB" was measuring corrupted vs corrupted)
   - Byte savings: **97%** (stable — encoder-side, unaffected)
5. **Diagnosed remaining visual artifacts**:
   - **Bright dots at coverage gaps**: Guided upsample bilateral filter isolates zero-coverage pixels from covered neighbors due to extreme color difference vs sigma_range. Confirmed: raw surfel render is clean; dots introduced by error correction path only.
   - **Overall blur vs source**: Inherent to ds8 error correction losing high-frequency detail. Not from post-processing (confirmed OFF).
6. **Wrote Packet 31.1** covering both artifact fixes

## 6. Next Steps (Priority Order)

### Immediate: Commit the fixes
The double-reprojection fix and diagnostic improvements should be committed. The decoder.cpp coverage-gap changes need validation first — they may or may not be ready to commit.

### Packet 31.1 — Coverage gap + blur fix
Ready for implementer. `docs/packet_31_1.md` has the full spec:
- 31.1.1: Weight-buffer bilinear fallback at zero-coverage pixels
- 31.1.2: Coverage-weighted error blending to preserve surfel texture

Note: `pgu_decoder.cpp` already has a partial implementation in the working tree. The implementer should validate and complete it, not start from scratch.

### Recalibrate failing test baselines
3 tests need baseline updates for real Sintel data: depth estimator correlation, DC delta scale sweep, RLZ scale sweep.

### Re-run ALL benchmarks
All historical numbers in `docs/implementer-report.md` are invalid (computed against either black frames or with the corrupted decoder). The report sections from Packet 29.1, 30.1, 30.2, and all guided upsample/zero-AC numbers need re-measurement.

### Bitrate roadmap (from memory)
The execution plan per `project_bitrate_roadmap.md`:
- P22: atlas integration (done)
- P23: downscaled + coarse spacing
- P24: metadata-only references
- P25: infinite GOP (branch merged)

## 7. Updated Benchmark Numbers

### Cluster motion benchmark — Sintel 30 (post-fix)

| Run | Config | Total bytes | Avg bytes/frame | Avg PSNR (dB) | Min PSNR (dB) | Savings vs Run A |
|---|---|---:|---:|---:|---:|---:|
| A | Reprojection + delta, no cluster motion | 32,276,617 | 1,075,887 | 47.4271 | 46.9677 | 0.0000% |
| B | Reprojection + delta + cluster motion | 33,142,112 | 1,104,737 | 46.3586 | 45.0167 | -2.6815% |
| C | Reprojection + delta + cluster motion + ds8 guided | 975,240 | 32,508 | 34.3784 | 32.6860 | 96.9785% |

### Zero-AC benchmark — Sintel 30 (post-fix)
All three configs (max_ac=255, 3, 0) produced identical output at ds8: 2,461,107 bytes, 37.60 dB. AC coefficient truncation has no effect at ds8 resolution — error blocks are too small for high-frequency AC coefficients to matter.

## 8. Architecture Overview

### Pipeline (current)
```
Input Frame
    |
    +-- estimate_grain() -> GrainParams          [strip pre-encode]
    +-- strip_grain() -> Clean Frame
    |
    +-- init_surfels_from_image() -> SurfelCloud
    +-- segment_surfel_cloud() -> labels
    +-- reproject_to_ellipsoids() -> Ellipsoid wrapping  [MODIFIES CLOUD IN-PLACE]
    +-- compute_cluster_signatures() -> atlas matching
    +-- atlas_.ingest_frame() -> persistent dictionary
    |
    +-- refine_surfel_colors() -> iterative color correction
    +-- SurfelRasterizer::render() -> Reconstructed Frame
    +-- ErrorAnalyzer::analyze() -> Dirty Blocks
    +-- ErrorPatchCompressor::compress() -> DCT + rANS
    +-- BitstreamWriter::finalize() -> Bitstream
    +-- save_surfel_cloud_v7() -> Cloud Bytes (I-frame)
    |
    +-- SequenceEncoder manages GOP lifecycle

Decode Side:
    +-- load_surfel_cloud_v7() (I-frame, needs AtlasDictionary)
    +-- BitstreamReader -> Error patches
    +-- SurfelRasterizer::render() -> Raw surfel frame
    +-- apply_error_layers() -> ds8 guided upsample + composite
    +-- refine_surfel_colors() -> update cloud for next frame
    +-- synthesize_grain() -> Final Frame (if post-process enabled)
```

### Critical invariant: `reproject_to_ellipsoids()` modifies cloud in-place
This function takes `SurfelCloud&` (non-const) and rewrites tangent vectors. Any code that needs the original cloud MUST copy it BEFORE calling this function. This was the root cause of both bugs found this session. The next director should watch for this pattern in any new code that touches ellipsoid/atlas processing.

### Key file counts
- 32 headers in `pgu_core/include/`
- 25 source files in `pgu_core/src/`
- 45 test files in `pgu_tests/src/`

## 9. Diagnostic Output Inventory

All generated with real Sintel 1080p data, post double-reprojection fix:
- `docs/diag_surfel_render/frame_%04d.ppm` — raw surfel rasterization (no error correction) — **NEW, clean baseline**
- `docs/diag_decoded/frame_%04d.ppm` — full decode pipeline output (has dot artifacts)
- `docs/diag_atlas/frame_%04d.ppm` — atlas entry coloring (RGBCMY for top 6 entries)
- `docs/diag_error/frame_%04d.ppm` — error patch heat maps
- `docs/decoded_frames_reprojection_delta/` — Run A benchmark decoded frames
- `docs/decoded_frames_cluster_motion/` — Run B benchmark decoded frames
- `docs/decoded_frames_cluster_motion_ds8/` — Run C benchmark decoded frames

## 10. Decision Log (This Session)

| ID | Decision | Rationale |
|----|----------|-----------|
| D-031 | Use copy for atlas signature computation in decoder | reproject_to_ellipsoids modifies cloud in-place; decoder was double-reprojecting |
| D-032 | Coverage gap fix via weight buffer bilinear fallback | Surgical fix at zero-coverage pixels without degrading guided upsample elsewhere |
| D-033 | ds8 blur to be addressed via coverage-weighted error blending | Reduce correction magnitude at well-covered pixels where surfel render is already sharp |

## 11. Memory System

Persistent memory at `/home/budai/.claude/projects/-home-budai-Projects-PGU/memory/`.
Key entries relevant to current work:
- `feedback_billboard_abandoned.md` — billboard path dropped due to seam error
- `project_bitrate_roadmap.md` — execution plan for 256kbps target
- `feedback_test_log.md` — implementer captures test output; auditor reads log
- `feedback_auditor_no_recommendations.md` — auditor describes issues only, never suggests fixes
- `feedback_audit_report_format.md` — re-audits update finding status, never overwrite with "No findings"
- `feedback_instance_routing.md` — same instance per functionality pillar
