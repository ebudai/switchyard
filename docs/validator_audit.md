# Validator Audit Report — Part 1, Lane A

**Methodology:** Adversarial-Collaborative Audit
**Branch Reviewed:** `lane-a/galaxy-part1-generation`
**Status:** Clean (All checklist items verified, no implementation defects found)

---

## Findings & Evidence

### 1. Determinism and Purity of `GenerateStar`
- **Finding:** `GenerateStar(const Galaxy&, uint32_t)` is a pure, deterministic function of its arguments (`galaxy.seed` and the `starIndex`).
- **Evidence:** 
  - There are no mutable global/static variables used inside `GenerateStar`.
  - All calls to `random01` utilize the local `seed`, `starIndex` (passed in as `index`), and a unique stream ID.
  - The loop in `RegenerateGalaxy` is a simple index-based call structure, rendering it completely order-independent and safe for parallelization if needed.

### 2. Deletion of Cosmic-Web Symbols
- **Finding:** All deleted cosmic-web structures, functions, and constants are completely removed from `main.cpp` with zero dangling references or dead code.
- **Evidence:**
  - Ripgrep searches for `CellCoord`, `VoronoiFeatures`, `GeneratedPoint`, `VoidCenterGrid`, `InsertNearestDistance`, `GetCellOrigin`, `GetVoidCenter`, `BuildVoidCenterGrid`, `EvaluateVoronoiFeatures`, `GeneratePointCandidate`, `Saturate`, `kPointCount`, `kPointSeed`, `kCellsPerAxis`, `kPointsPerCell`, and `kCandidateBudgetPerCell` return zero occurrences.
  - The helper `Saturate` was confirmed to be completely removed, and is not used anywhere else in the codebase.

### 3. Signature Change and Stream Uniqueness of `random01`
- **Finding:** The signature change of `random01` to `(seed, index, stream)` was updated consistently across all call sites, and the stream IDs are strictly unique per logical random variable in any single execution path.
- **Evidence:**
  - Every call site of `random01` in `main.cpp` is located inside `GenerateStar` and adheres to the new signature.
  - The stream allocation within the execution paths is mutually exclusive and free of overlaps:
    - **Bulge path:** stream 0 (component decision), stream 2 (radial distance), stream 3 (theta), streams 4 & 5 (triangular vertical thickness), stream 6 (brightness), stream 7 (radius), stream 15 (color tint).
    - **Arm path:** stream 0 (component decision), stream 1 (arm/disk split), stream 2 (radial distance), stream 8 (arm index), stream 9 (arm angular jitter), stream 10 (arm radial jitter), stream 11 (brightness), stream 12 (radius), streams 13 & 14 (triangular vertical thickness), stream 15 (color tint).
    - **Disk path:** stream 0 (component decision), stream 1 (arm/disk split), stream 2 (radial distance), stream 8 (disk angle), stream 11 (brightness), stream 12 (radius), streams 13 & 14 (triangular vertical thickness), stream 15 (color tint).
  - No stream ID is reused for different variables along the same path, avoiding correlated sequences.

### 4. World-Space Quad Construction in Splat Shader
- **Finding:** The NDC-sizing bug is fully resolved. Splat quads are now built in world space and project correctly.
- **Evidence:**
  - `kSplatShader`'s `vs_main` constructs `world_corner` using the shared camera-relative `center`, the `quad_corner`, and the `tangent`/`bitangent` directions multiplied by `radius`.
  - The final position is calculated via `camera.view_proj * vec4<f32>(world_corner, 1.0)`.
  - The perspective divide `(x/w, y/w, z/w)` is handled by the GPU hardware, causing surfels to scale correctly with camera distance and foreshorten correctly when viewed at a tilted angle.

### 5. `CameraUniforms` Struct Extension and WGSL Layout
- **Finding:** `CameraUniforms` was correctly extended from 96 to 144 bytes. The memory layout matches std140 rules perfectly in both C++ and WGSL.
- **Evidence:**
  - C++ `CameraUniforms` has a `static_assert` verifying `sizeof(CameraUniforms) == 144`.
  - Each `vec3` member (`spinAxis`, `tangent`, `bitangent`) is manually padded to 16-byte boundaries via trailing dummy variables in C++: `_spinAxisPad`, `_tangentPad`, and `_bitangentPad`.
  - Both WGSL copies in `kHardwarePointShader` and `kSplatShader` declare `spin_axis`, `tangent`, and `bitangent` as `vec3<f32>` fields, which are automatically aligned to 16-byte boundaries under WebGPU/WGSL rules.
  - `queue_.WriteBuffer` uploads the entire struct using `sizeof(cameraUniforms_)` (144 bytes).

### 6. Orthonormalization Edge Case in `UpdateGalaxyFrame`
- **Finding:** Gram-Schmidt orthonormalization in `UpdateGalaxyFrame` correctly avoids degenerate/zero-length vectors for any 3D orientation.
- **Evidence:**
  - The function dynamically selects its reference vector: `Vec3{0, 1, 0}` when `std::abs(spin.y) < 0.99f`, and `Vec3{1, 0, 0}` otherwise.
  - This ensures that the angle between the normalized `spinAxis` and the reference vector is at least `acos(0.99) \approx 8.1` degrees, keeping the magnitude of their cross product at $\ge 0.141$ and preventing division by zero during normalization.

### 7. Runtime-Resizable GPU Buffers in `RegenerateGalaxy`
- **Finding:** GPU buffers are recreated only when `starCount` exceeds capacity, and are correctly reused via `queue_.WriteBuffer` otherwise.
- **Evidence:**
  - Gated by `if (starCount > pointBufferCapacity_ || pointPositionBuffer_ == nullptr)`, both `pointPositionBuffer_` and `pointStaticBuffer_` are recreated only when the buffer is unallocated or when the new `starCount` exceeds the tracked `pointBufferCapacity_`.
  - For smaller or equal counts, the existing buffers are safely reused via `queue_.WriteBuffer` writing exactly `starCount` elements.
  - Assigning new `wgpu::Buffer` handles to the class members automatically releases the previous WebGPU buffer resources.

### 8. Timing of `galaxyDirty_` Check
- **Finding:** The `galaxyDirty_` flag is consumed at the top of `RenderFrame()` before any rendering operations occur, preventing frames from rendering with stale or partially updated data.
- **Evidence:**
  - `galaxyDirty_` check is the first major operation in `RenderFrame()`. If set, it regenerates the galaxy frame, updates coordinates, writes attributes, and schedules position and camera uniform updates all before starting the render pass.

### 9. Dynamic Star Count Usage
- **Finding:** `pass.Draw` calls and window title updates dynamically use `galaxy_.starCount` instead of hardcoded constants.
- **Evidence:**
  - `pass.Draw` is invoked with `galaxy_.starCount` (or `6, galaxy_.starCount` for splats).
  - `UpdateWindowTitle` correctly formats the output string using `galaxy_.starCount`.

### 10. Procedural Star Generation Math
- **Finding:** The generation algorithm closely matches the architecture design guidelines in substance and mathematical formulation.
- **Evidence:**
  - Inverse transform sampling is used correctly to model the exponential radial falloff: `r = -scaleLength * std::log(1.0f - rDraw)`.
  - Spiral arm placement uses `spiralAngle = std::log(r / innerRadius) / spiralB` (where `spiralB = std::tan(pitchAngle)`), which perfectly implements the logarithmic spiral formula $r = a \cdot e^{b\theta}$.
  - Bulge stars use circular symmetric angular distributions and vertical thickness scaling is correctly set to `1.8f * diskThickness` (larger than disk's `1.0f * diskThickness`).

### 11. Bounded Palette Color Blending
- **Finding:** Color blending math has zero risk of NaN or out-of-range values.
- **Evidence:**
  - `weightSum` is clamped to $\ge 1.0\times 10^{-4}$ via `std::max`, eliminating division-by-zero risks.
  - `tint` is bounded within `[0.9, 1.1]`.
  - The final color is clamped component-wise to `[0.0, 1.0]` using `ClampColor`.

### 12. Floating-Origin Precision Compatibility
- **Finding:** Orthonormal tangent vectors being pure directions makes them invariant to camera translation, making the world-space splat orientation fully compatible with the floating-origin scheme.
- **Evidence:**
  - Translation is applied in double-precision on the CPU: `relative = position - cameraPosition_`.
  - Tangents/bitangents are pure direction vectors. Adding a quad displacement in tangent-bitangent directions is translation-invariant and mathematically identical in both absolute world coordinates and camera-relative coordinates.

### 13. Sanity-Check on Frame-Rate Limit
- **Finding:** There are no hidden $O(N)$ or worse CPU operations running per-frame during static camera frames. The ~155-160 FPS ceiling is purely present/fill-rate/VSync bound.
- **Evidence:**
  - The $O(N)$ position update `UpdatePointPositionBuffer()` is skipped when the camera is static.
  - The present mode prefers `Fifo` (VSync-bound) or `Mailbox`/`Immediate` depending on the system/compositor capabilities. A VSync barrier is the cause of the FPS cap.

### 14. File Scope Constraints
- **Finding:** Only `main.cpp` and `docs/implementer-report.md` were modified on the branch compared to the common ancestor.
- **Evidence:**
  - Verified using `git diff <ancestor> lane-a/galaxy-part1-generation --name-only`.

### 15. Compile and Warnings
- **Finding:** The project builds cleanly with zero warnings under `-Wall -Wextra -Wpedantic`.
- **Evidence:**
  - Confirmed by a fresh clean compilation using `cmake --build build -j$(nproc)`.

---

## Open Questions

1. **Target Frame Rate vs Display VSync:** 
   Confirm whether the 240 FPS target is a hard requirement for the target deployment machines, or if the current display/compositor VSync limit (~155-160 FPS on this X11 setup) is acceptable.
2. **Lane B Interface Coordination:** 
   Lane B's ImGui panel should drive the orientation through `spinAxis` only, since `UpdateGalaxyFrame()` automatically re-derives `tangent` and `bitangent` from it (overwriting any manual UI changes to those tangent vectors).
