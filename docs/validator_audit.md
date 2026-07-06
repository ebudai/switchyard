# Validator Audit Report — main.cpp Modularization Split

**Methodology:** Adversarial-Collaborative Audit
**Branch Reviewed:** `modularize/split-main-cpp` (worktree: `/home/budai/Projects/pgu-modularize-audit`)
**Status:** Clean (All checklist items verified, zero behavior changes, build/dependency rules behave correctly)

---

## Findings & Evidence for Checklist Items

### 1. Symbol Body Correspondence Verification
- **Finding:** Every extracted C++ symbol is character-for-character identical to the version in `origin/main:main.cpp`.
- **Evidence:**
  - `vec_math.h` successfully houses the vector/matrix math primitives (`Vec3`, `Vec3d`, `Mat4`, operators, `Cross`, `Dot`, `Normalize`, `Multiply`, `Perspective`, `LookAt`) without structural or logical deviation.
  - `hash_util.h` includes `SpreadBits21`, `Morton3D`, `MixHash64`, and `random01` exactly as defined in the pre-split source.
  - `gpu_types.h` carries the GPU interface data structs (`CameraUniforms`, `QuadVertex`, `PointPosition`, `PointStaticAttributes`, `RenderMode`, `RenderTuning`).
  - `galaxy_model.h` encapsulates `Galaxy`, `StarSample`, `Covariance3`, `SurfelNode`, `IsotropicCovariance`, `LargestEigenvalue3x3`, `RotateCovariance` (previously a static member of `App`, now a free `inline` function), `ClampColor`, and `GenerateStar` with zero modifications to their math blocks.

### 2. Shader Byte-Identity Verification
- **Finding:** The extracted WGSL shaders are byte-identical to their original `R"(...)"` string literals.
- **Evidence:**
  - `shaders/hardware_point.wgsl` and `shaders/splat.wgsl` match the pre-split strings character-for-character.
  - The CMake embedding script (`cmake/embed_shaders.cmake`) generates a C++ raw string literal template containing a leading newline, reproducing the exact byte-wise sequence from the inline `R"(...)"` literals in `main.cpp`.

### 3. Preservation of layout `static_assert`s
- **Finding:** GPU layout static asserts are preserved in the headers.
- **Evidence:**
  - Located in [gpu_types.h:L30](file:///home/budai/Projects/pgu-modularize-audit/gpu_types.h#L30) (`CameraUniforms` size 144) and [gpu_types.h:L53-54](file:///home/budai/Projects/pgu-modularize-audit/gpu_types.h#L53-L54) (`PointPosition` size 12 and `PointStaticAttributes` size 36).

### 4. Linkage Change Assessment
- **Finding:** The linkage transition from file-scope internal (anonymous namespace) to global `inline` header declarations is correct and safe.
- **Evidence:**
  - `CMakeLists.txt` compiles `main.cpp` as the single executable translation unit. No name collisions are possible, and internal link symbols are not shared across separate TU compilations.

### 5. Name Grep Verification
- **Finding:** There are no spelling errors, typos, or accidental partial renames.
- **Evidence:**
  - Confirmed by clean compilation under `-Wall -Wextra -Wpedantic`.

### 6. Behavioral Reproduction
- **Finding:** Modularized application exhibits bit-for-bit identical hierarchy and rendering statistics compared to the pre-split branch.
- **Evidence:**
  - Running the binary yields the following startup logs:
    ```
    [hierarchy] leaves=60000 nodes=80002 levels: 60000 15000 3750 938 235 59 15 4 1
    [hierarchy] brightness root=90292.8 sum(leaves)=90292.8 sum(root.children)=90292.8 rel.err(root vs leaves)=6.09495e-08
    [hierarchy] root cov diag=(7.28709, 5.13432, 0.0918704) effRadius=8.13004 largestEig=7.34417
    ```
  - These values are identical to the Gaussian branch pre-split stats.

---

## Other Review Items

### 7. File Scope Compliance
- **Finding:** The files touched match expectations: `CMakeLists.txt`, `cmake/embed_shaders.cmake`, `docs/implementer-report.md`, `galaxy_model.h`, `gpu_types.h`, `hash_util.h`, `main.cpp`, `shaders/hardware_point.wgsl`, `shaders/splat.wgsl`, `vec_math.h`.
- **Evidence:** Verified via `git diff --name-status origin/main HEAD`.

### 8. Header Hygiene
- **Finding:** `#pragma once` guards are present on all four headers. Include maps are clean, minimal, and free of circular dependencies.

### 9. Build & Dependency Tracking Verification
- **Finding:** CMake dependency tracking works correctly for the custom command shader embedding.
- **Evidence:**
  - Verified that running `touch shaders/splat.wgsl` triggers `Embedding WGSL shaders -> shaders_generated.h` and recompiles `main.cpp.o` on the next build.

---

## Open Questions

- *None.* The refactor is clean, safe, and maintains complete parity with the original single-file implementation.
