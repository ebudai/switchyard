# Validator Audit Report — App Class Decomposition Refactor

**Methodology:** Adversarial-Collaborative Audit
**Branch Reviewed:** `decompose/app-class` (worktree: `/home/budai/Projects/pgu-decompose-audit`)
**Status:** Clean (All checklist items verified, behavior-preserving deviations confirmed safe, no defects introduced)

---

## Verification of Behavior-Preserving Deviations

### 1. RenderFrame Reorder Safety
- **Analysis:** In `App::RenderFrame`, `galaxy_.RegenerateIfDirty()` and `galaxy_.BuildCut()` run prior to the GPU render pass `renderer_.RenderFrame()`. 
  - `RegenerateIfDirty()` and `BuildCut()` operate purely on the CPU, and do not access the GPU depth buffer or the configured swapchain surface.
  - The framebuffer size they query (`renderer_.FramebufferWidth()` and `renderer_.FramebufferHeight()`) is updated by the GLFW resize callback (`Renderer::OnFramebufferResize`), which executes during `glfwPollEvents()` at the start of the frame loop, before `App::RenderFrame()` runs.
  - The actual GPU surface reconfigure `ConfigureSurface()` happens conditionally on `framebufferResized_` at the start of `Renderer::RenderFrame()`, prior to `UpdateCameraUniforms()` and the draw encoder.
- **Conclusion:** Reordering is completely inert. The CPU-side data generation reads already-current dimensions, and the GPU surface is configured before any draw commands execute.

### 2. Title-Update Call-Count Collapse
- **Analysis:** `UpdateWindowTitle()` is a pure formatting function with no side effects beyond modifying the GLFW window title.
- **Conclusion:** Because the function is idempotent, calling it once per frame (after either a render mode toggle or a tuning parameter change occurs) produces a final title string identical to the one produced by the multiple calls in the monolithic implementation.

### 3. Shutdown Order Integrity
- **Analysis:** The shutdown sequence executes:
  1. `panel_.Shutdown()` — tears down the ImGui context and backend.
  2. `renderer_.Shutdown()` — releases all WGPU resources and sets handles to `nullptr`.
  3. `glfwDestroyWindow(window_)` and `glfwTerminate()`.
- **Conclusion:** ImGui is torn down while the WGPU device and queue it references are still alive. This matches the original monolithic shutdown sequence exactly, avoiding any use-after-free or WGPU validation errors.

---

## Architectural Rules Compliance

### 4. `GalaxySystem` WebGPU Agnosticism
- **Finding:** `GalaxySystem` is completely decoupled from the WebGPU API.
- **Evidence:**
  - `galaxy_system.h` and its transitive dependencies (`galaxy_model.h`, `hash_util.h`, `vec_math.h`, and `gpu_types.h`) do not include `<webgpu/*>` or reference `wgpu::` types.
  - CPU-side traversal output buffers are stored as standard `std::vector`s of plain C++ structs.

### 5. No App Back-References
- **Finding:** Sub-objects have no dependencies or references back to the `App` orchestrator.
- **Evidence:**
  - A search of all sub-object headers for `App&` or `App*` yields zero matches. All cross-component variables are passed strictly as parameters at call time. The only borrowed handle is `Renderer`'s non-owning `GLFWwindow*` reference.

---

## Findings & Evidence for Checklist Items

### 6. Bit-for-Bit Telemetry Verification
- **Finding:** The decomposed implementation produces telemetry outputs that are byte-identical to the baseline Gaussian branch.
- **Evidence:**
  - Running the binary prints:
    ```
    [hierarchy] leaves=60000 nodes=80002 levels: 60000 15000 3750 938 235 59 15 4 1
    [hierarchy] brightness root=90292.8 sum(leaves)=90292.8 sum(root.children)=90292.8 rel.err(root vs leaves)=6.09495e-08
    [hierarchy] root cov diag=(7.28709, 5.13432, 0.0918704) effRadius=8.13004 largestEig=7.34417
    ```
  - Default view window title: `60000 gen / 8289 surfels`.

### 7. Sanity Check of Ambiguous Boundary Decisions
- **Finding:** The three engineering trade-offs made are sound and correct:
  - Consolidating framebuffer size query/callback paths to `Renderer` keeps size states unified.
  - Duplicating the simple one-line helper `Fail` avoids cluttering the header landscape with tiny utility headers.
  - Per-call parameter passing maximizes decoupling.

### 8. File Scope Compliance
- **Finding:** Scope boundaries were respected.
- **Evidence:** Only `main.cpp`, `camera_controller.h`, `galaxy_system.h`, `renderer.h`, `tuning_panel.h`, `gpu_types.h`, and `docs/implementer-report.md` were touched.

### 9. Build and Warnings Verification
- **Finding:** Compilation is clean with zero warnings under `-Wall -Wextra -Wpedantic`.

### 10. Incremental Commit Structure
- **Finding:** The refactor was committed in a clear, incremental sequence.
- **Evidence:**
  - `9de8599` Extract CameraController
  - `dd9728d` Extract GalaxySystem
  - `dd6d1aa` Extract TuningPanel
  - `6ee19ee` Extract Renderer
  - `369fa59` Add report

---

## Open Questions

- *None.* The decomposition achieves a clean separation of concerns without introducing regressions.
