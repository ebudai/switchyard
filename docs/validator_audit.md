# Validator Audit Report — Aspect-Ratio Guard

**Methodology:** Adversarial-Collaborative Audit
**Branch Reviewed:** `fix/aspect-ratio-guard` (worktree: `/home/budai/Projects/pgu`)
**Status:** Clean (All checklist items verified, regression test committed and verified)

---

## Findings & Evidence for Checklist Items

### 1. Independent Re-derivation of the Diagnosis
- **Finding:** The diagnosis of the aspect-ratio streaking bug is correct.
- **Evidence:**
  - In the baseline `UpdateEwaProjectionState` (in `galaxy_system.h`), the aspect ratio computation was guarded only against `framebufferHeight <= 0`. If `framebufferHeight > 0` but `framebufferWidth == 0` (which transiently occurs during a resize), `aspect = 0.0f`.
  - The EWA projection computes `projFOverAspect_ = projF_ / aspect`, causing a division by zero and setting `projFOverAspect_` to positive infinity (`inf`).
  - In `EmitSurfel`, the Jacobian terms `j00 = -projFOverAspect_ * invTz` and `j02 = projFOverAspect_ * tx * invTz * invTz` scale directly with `projFOverAspect_`.
  - When `projFOverAspect_` becomes infinite, the 2x2 screen-space covariance matrix elements ($a$, $b$, $c$) become infinite or NaN, making the larger eigenvalue $e_1$ infinite.
  - The major axis half-extent `half1 = 3.0f * std::sqrt(e1)` then becomes infinite, scaling `axis1` and `axis2` to infinity and causing the massive streaking across the window.
  - In `renderer.h`'s `UpdateCameraUniforms`, the function returns early if the width/height are non-positive. However, a small-but-positive width (e.g. 1px) still creates an extreme finite aspect ratio that distorts the perspective projection matrix on the GPU, while the CPU-side EWA path (which lacked an early-return) still computes an extremely skewed or infinite aspect.

### 2. Verify the Fix's Clamp Bounds Match Exactly
- **Finding:** The clamp bounds match exactly between both files.
- **Evidence:**
  - In `galaxy_system.h` (line 419):
    `const float aspect = std::clamp(rawAspect, 1.0f / 8.0f, 8.0f);`
  - In `renderer.h` (line 339):
    `const float aspect = std::clamp(static_cast<float>(framebufferWidth_) / static_cast<float>(framebufferHeight_), 1.0f / 8.0f, 8.0f);`
  - Keeping these bounds identical guarantees that the CPU-side EWA screen axes and the GPU-side view-projection matrix agree on the aspect ratio under all circumstances.

### 3. Normal-Case Equivalence
- **Finding:** The clamp is a true no-op for any standard window size.
- **Evidence:**
  - The aspect ratio bounds `[1/8, 8]` cover typical window sizes (e.g. 800x600 aspect is `1.333`, 1920x1080 aspect is `1.778`, which are both well within `[0.125, 8.0]`).
  - Both baseline and fixed versions produce byte-identical surfel axis attributes for standard cases:
    - **800x600:** Baseline max axis = `0.198615`, Fixed max axis = `0.198615` (identical).
    - **1920x1080:** Baseline max axis = `0.198468`, Fixed max axis = `0.198468` (identical).

### 4. Sanity Check the Bound Choice
- **Finding:** The choice of `[1/8, 8]` is highly reasonable.
- **Evidence:**
  - It admits all realistic window shapes (e.g. 32:9 super-ultrawide displays have aspect `3.56`, portrait windows have aspect `0.43` or `0.375`).
  - It prevents zero, negative, or infinite values for `aspect`, keeping the aspect ratio bounded inside $(0, \infty)$.
  - Under the tightest clamp limit (`aspect = 1/8`), the maximum axis magnitude is safely bounded to `~1.56` NDC units, which prevents runaway streaking.

### 5. Scope and Rules Verification
- **Finding:** Only `galaxy_system.h`, `renderer.h`, and `docs/implementer-report.md` were modified by the implementer.
- **Evidence:** Verified via `git diff --name-only origin/main HEAD`.
- **Finding:** `galaxy_system.h` remains WebGPU-agnostic.
- **Evidence:** No `<webgpu/*>` includes or backend-specific dependencies were added to `galaxy_system.h`.

### 6. Build Status
- **Finding:** Clean rebuild completed successfully with zero warnings/errors under `-Wall -Wextra -Wpedantic`.

---

## Regression Test Results

We created, verified, and committed a dedicated regression test: [tests/aspect_ratio_clamp_test.cpp](file:///home/budai/Projects/pgu/tests/aspect_ratio_clamp_test.cpp).

### 1. Verification Against Baseline Code (Pre-fix)
- **Status:** **FAILED** (exit code 1)
- **Test Output:**
  ```
  Test Case: 800x600 (Normal)
    Surfels: 3323
    Max Axis Magnitude: 0.198615
    Has Non-Finite: NO
    Status: SANE

  Test Case: 1920x1080 (Normal)
    Surfels: 3323
    Max Axis Magnitude: 0.198468
    Has Non-Finite: NO
    Status: SANE

  Test Case: 1x800 (Pathologically Narrow)
    Surfels: 3323
    Max Axis Magnitude: 155.958
    Has Non-Finite: NO
    Status: CORRUPT

  Test Case: 0x800 (Zero Width)
    Surfels: 3323
    Max Axis Magnitude: inf
    Has Non-Finite: YES
    Status: CORRUPT

  Overall result: CORRUPT (regression detected/fix missing)
  ```
- **Conclusion:** The test successfully catches the bug under pathologically narrow/zero-width viewports on the baseline code.

### 2. Verification Against Fixed Code (Post-fix)
- **Status:** **PASSED** (exit code 0)
- **Test Output:**
  ```
  Test Case: 800x600 (Normal)
    Surfels: 3323
    Max Axis Magnitude: 0.198615
    Has Non-Finite: NO
    Status: SANE

  Test Case: 1920x1080 (Normal)
    Surfels: 3323
    Max Axis Magnitude: 0.198468
    Has Non-Finite: NO
    Status: SANE

  Test Case: 1x800 (Pathologically Narrow)
    Surfels: 3323
    Max Axis Magnitude: 1.5596
    Has Non-Finite: NO
    Status: SANE

  Test Case: 0x800 (Zero Width)
    Surfels: 3323
    Max Axis Magnitude: 1.5596
    Has Non-Finite: NO
    Status: SANE

  Overall result: SANE (all tests passed)
  ```
- **Conclusion:** The aspect-ratio guard successfully clamps pathological cases to a safe bound of `~1.56` max axis magnitude while keeping standard cases perfectly identical.
