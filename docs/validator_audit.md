# Validator Audit Report — Resize Splat Corruption (Present-Path Shear)

**Methodology:** Adversarial-Collaborative Audit
**Branch Reviewed:** `fix/resize-splat-corruption` (worktree: `/home/budai/Projects/pgu`)
**Status:** Clean (All checklist items verified, regression test verified and passed on fix branch, failed on pre-fix main branch, zero build warnings)

---

## Findings & Evidence for Checklist Items

### 1. Sanity-Check the Mechanism Claim
* **Analysis:** The diagnosis of a virtio-gpu / Mesa virgl present-path row-pitch alignment mismatch is mathematically sound and internally consistent.
* **Evidence:**
  * **Arithmetic Verification:** The swapchain textures use the `BGRA8` format (4 bytes per pixel). With a 256-byte linear row pitch constraint (standard for Virtio-GPU Venus/Mesa host-guest interfaces), the alignment boundary is:
    $$\frac{256 \text{ bytes}}{4 \text{ bytes/pixel}} = 64 \text{ pixels}$$
  * **Shear Physics:** When the presented window width $W$ is not a multiple of 64, the guest-side texture is allocated with a pitch of $W_{\text{aligned}} = \lceil W / 64 \rceil \times 64$ pixels. However, the host scanout reads each row at the unpadded width $W$. This results in a cumulative shift of $W_{\text{aligned}} - W$ pixels per row, producing a diagonal shear/streaking effect.
  * **Data Consistency:** The implementer's instrumentation (checking camera basis, uniform buffers, and projection parameters) confirmed that CPU-side state remains 100% correct in the corrupted state. The surface texture size also matches the framebuffer size. This rules out application-level bugs and points directly to the virtualized present path.

### 2. Verify the Fix Mechanism in `main.cpp`
* **Analysis:** The snapping logic in `FramebufferSizeCallback` is correct, safe, and robust.
* **Evidence:**
  * **Recursion & Liveness:** When an unaligned width is snapped via `glfwSetWindowSize(window, alignedWidth, height)`, it re-triggers the callback (either synchronously or on the next event loop tick). Since `alignedWidth` is already a multiple of 64, the second pass computes `alignedWidth == width`, bypassing the snap condition. This ensures it recurses at most once, with zero risk of an infinite loop.
  * **Cold Start Safety:** The constant `kInitialWidth` is `1280`, which is divisible by 64 ($1280 / 64 = 20$). Thus, the initial window begins aligned, and no callback or resizing is required during cold start.
  * **Zero/Negative Prevention:** The logic uses `std::max(64, (width/64)*64)` inside an `if (width > 0)` check. Any positive width will result in an aligned width of at least `64`, preventing zero or negative values.

### 3. Verify `glfwSetWindowSize`'s Re-entrancy Assumption
* **Analysis:** In the GLFW X11 backend (`third_party/glfw/src/x11_window.c`), calling `glfwSetWindowSize` invokes `XResizeWindow` and `XFlush` asynchronously. The new size event (`ConfigureNotify`) is processed during event polling (`glfwPollEvents`), triggering the callback asynchronously.
* **Safety & Correctness:** 
  * The fix remains fully correct under this asynchronous flow. On the unaligned pass, the callback requests the aligned size and early-returns without notifying the renderer swapchain.
  * During the subsequent event loop tick, the aligned size event is processed, triggering `FramebufferSizeCallback` with the aligned size, which then propagates to the renderer. Thus, the renderer is never exposed to the unaligned/sheared state.

### 4. Reproduce the Regression Test
* **Status:** Verified and PASSED.
* **Evidence:**
  * **Baseline (Pre-fix `origin/main`):** **FAIL** (exit code 1)
    * `requested_w=1400 presented_w=1400 shear_ratio=2.077 -> SHEARED`
    * `requested_w=1000 presented_w=1000 shear_ratio=2.392 -> SHEARED`
    * `requested_w=1300 presented_w=1300 shear_ratio=2.928 -> SHEARED`
  * **Fixed (`fix/resize-splat-corruption`):** **PASS** (exit code 0)
    * `requested_w=1400 presented_w=1344 shear_ratio=0.924 -> ok`
    * `requested_w=1000 presented_w=960  shear_ratio=0.901 -> ok`
    * `requested_w=1300 presented_w=1280 shear_ratio=0.921 -> ok`
  * The regression test is highly robust, successfully reproducing and detecting the shear via ImageMagick Sobel edge analysis on unaligned widths, and confirming clean renders on aligned widths.

### 5. File Scope & Untouched Check
* **File Scope:** Verified via `git diff --name-only origin/main HEAD`. The modified files are limited to:
  * [main.cpp](file:///home/budai/Projects/pgu/main.cpp) (snapping implementation)
  * [tests/resize_present_shear_test.sh](file:///home/budai/Projects/pgu/tests/resize_present_shear_test.sh) (regression test)
  * [docs/implementer-report.md](file:///home/budai/Projects/pgu/docs/implementer-report.md) (documentation)
  * `scripts/director_watchdog.py` (an unrelated, already-committed file on `main` that this branch predates).
* **Untouched Files:** Verified via `git diff origin/main HEAD -- renderer.h galaxy_system.h`. The output was empty, confirming that all temporary instrumentation in these files was fully reverted.

### 6. Build Status
* **Rebuild Command:** `rm -rf build && cmake -S . -B build && cmake --build build -j$(nproc)`
* **Result:** Clean compilation with zero compiler warnings or errors.

---

## Residual Risks & Recommendations

1. **HiDPI/Fractional Scaling:** Deriving the window size coordinates directly from the framebuffer pixel coordinates assumes a content scale factor of $1.0$ (standard X11 setup). If fractional scaling is introduced, the snapped width in pixels must be converted to window coordinates using `glfwGetWindowContentScale` to prevent incorrect window dimensioning.
2. **Backend/Driver Dependency:** Since this is a workaround for a Virtio-GPU/Mesa Venus present bug, the snap behavior is technically redundant on bare-metal systems or bug-free drivers. However, the performance/UX cost of snapping the width in 64px increments is negligible and provides general driver-level safety.
