# Validator Audit Report — In-App Screenshot Capture (Retroactive)

**Methodology:** Adversarial-Collaborative Audit (retroactive — commit already on `main`)
**Commit Reviewed:** `d43adbc` "Add in-app screenshot capture" (`main.cpp`, `renderer.h`)
**Author:** Codex instance in pgu-ui, pushed directly to `main` (process gap, not itself a code issue)
**Status:** **Needs Fix** — one likely-real correctness bug in the readback timing, plus a hang-risk and a minor parsing rough edge. Everything else checked out.

---

## 1. Findings

### 1.1 [High / plausible] Screenshot readback is encoded *after* `surface_.Present()`, against the same presented texture

In `renderer.h::RenderFrame()`:

```cpp
if (surface_.Present() != wgpu::Status::Success) { ... return {}; }

FrameResult result;
result.presented = true;
if (pendingScreenshotPath_.has_value()) {
    CapturePresentedSurfaceTexture(surfaceTexture.texture, *pendingScreenshotPath_);
    ...
}
```

`CapturePresentedSurfaceTexture` builds a *new* command encoder, does `CopyTextureToBuffer(surfaceTexture.texture, ...)`, and submits it — all after `Present()` already returned success for that same texture.

This is backwards relative to how WebGPU/Vulkan swapchains work. Once `Present()` succeeds, ownership of that swapchain image transfers to the presentation engine (in the Vulkan backend, that's the `vkQueuePresentKHR` handoff); the application isn't supposed to issue further GPU commands against it until it's reacquired via `GetCurrentTexture()` on a later frame. Recording a `CopyTextureToBuffer` against an already-presented swapchain texture is at best undefined (garbage/black pixels if the backend tolerates it) and at worst a validation error that Dawn's default uncaptured-error handling can turn into a hard abort — which would take down the exact automated capture path this feature exists for.

**Why this matters here specifically:** commit `5058d21` (same day) added kwin/xdotool/imagemagick tooling to the team container *specifically so `--screenshot-out`/F12 could produce real output for headless GUI testing*. If this path corrupts or crashes instead of writing a clean PNG, the thing meant to validate the app visually is itself broken.

**Fix:** encode the `CopyTextureToBuffer` into the *same* command buffer as the render pass — right after `pass.End()`, before `encoder.Finish()` — so it executes before `Present()` is ever called (WebGPU auto-tracks the render-pass-write → copy-read hazard within one encoder, no manual barrier needed). Only the buffer map + PNG write (which touches the readback buffer, not the swapchain texture) needs to stay after `Present()`.

**Not verified live:** this container has no DISPLAY/XDG_RUNTIME_DIR either (confirmed: `/tmp/pgu_screenshot_verify.log` shows `GLFW Error (65550): Failed to detect any supported platform`), so I could not reproduce an actual crash or inspect a real captured PNG. This finding is from static analysis of WebGPU/Vulkan swapchain-ownership semantics, not a reproduced failure. Recommend exercising `--screenshot-out=/tmp/x.png --screenshot-after-frames=5` for real once the new kwin headless compositor tooling is available, and diffing/inspecting the output, as the actual acceptance bar.

### 1.2 [Medium] No bound on waiting for `screenshotAfterFrames` presented frames — can spin forever

The auto-capture check in `main.cpp::MainLoop()`:

```cpp
if (options_.screenshotOutputPath.has_value() &&
    !renderer_.ScreenshotPending() &&
    presentedFrames_ + 1 >= options_.screenshotAfterFrames) {
    QueueScreenshot(*options_.screenshotOutputPath, true);
}
```

only advances `presentedFrames_` when `RenderFrame()` actually presents. If the surface never presents a frame — minimized at startup, or `GetCurrentTexture` perpetually returning `Timeout`/`Outdated` in a headless/virtual-compositor setup (this repo already has documented virtio-GPU/venus quirks around present) — the main loop just spins indefinitely waiting to reach the target count, with no error or timeout path. Given the whole point of this flag is unattended CI/script use, a silent hang here wedges the calling script instead of failing loudly with a diagnosable message.

Suggest a frame-attempt or wall-clock cap (e.g. "if N seconds/iterations pass with zero presented frames while a screenshot is outstanding, `Fail()` with a clear message") rather than spinning forever.

### 1.3 [Low / quality] `parseUint32`'s crafted error message can be bypassed by `stoull`'s own exceptions

```cpp
auto parseUint32 = [](const std::string& value, const char* flagName) {
    std::size_t consumed = 0;
    const unsigned long long parsed = std::stoull(value, &consumed, 10);
    if (consumed != value.size() || parsed == 0 || parsed > std::numeric_limits<uint32_t>::max()) {
        throw std::runtime_error(std::string("Invalid value for ") + flagName + ": " + value);
    }
    return static_cast<uint32_t>(parsed);
};
```

`std::stoull` itself throws `std::invalid_argument`/`std::out_of_range` for non-numeric or overflowing-`unsigned long long` input, before the function's own "Invalid value for ..." message can fire. Verified:

```
$ ./pgu --screenshot-out=/tmp/x.png --screenshot-after-frames=abc
stoull
$ ./pgu --screenshot-out=/tmp/x.png --screenshot-after-frames=99999999999999999999
stoull
```

Still exits 1 and is still caught by `main`'s generic `catch (const std::exception&)`, so this isn't a functional bug — just a rough edge in an otherwise carefully-messaged parser. Wrap the `stoull` call in try/catch and rethrow with the same `flagName`-qualified message for consistency.

---

## 2. What checked out

* **Row-padding/stride math:** `AlignTo(width * 4, 256)` for `bytesPerRow`, buffer sized `paddedBytesPerRow * height` — correct per the WebGPU 256-byte-alignment requirement, and the per-row de-pad loop before `stbi_write_png` (stride = unpadded width) is correct.
* **BGRA→RGBA channel remap:** correct index mapping for `BGRA8Unorm(Srgb)`; direct `memcpy` for the `RGBA8Unorm(Srgb)` case (matches the surface's own format-selection fallback in `ConfigureSurface`).
* **`CopySrc` usage flag is capability-gated:** `surfaceSupportsCopySrc_` is only set from `capabilities.usages`, and `config.usage` only OSes in `CopySrc` when supported — won't request an unsupported flag or break configuration on backends lacking it. `CapturePresentedSurfaceTexture` fails loudly (not silently) if the surface lacks `CopySrc`.
* **CLI parsing doesn't regress plain startup:** `./pgu` with no args → default `AppOptions`, no exceptions, unchanged behavior. `./pgu --help` exits 0 before any window/device creation.
* **Edge cases tested directly** (all exit 1 with a sensible message, no crash): unknown flag, `--screenshot-after-frames` without `--screenshot-out`, empty `--screenshot-out=`, `--screenshot-after-frames=0`, negative value, missing value for `--screenshot-out`.
* **No premature exit before a frame is presented:** `exitAfterPendingScreenshot_` can only lead to `glfwSetWindowShouldClose` after `frameResult.screenshotWritten`, which is only set after `result.presented = true` was already assigned earlier in the same `RenderFrame()` call.
* **Build:** incremental `cmake --build build-native` after touching both changed files compiles clean, zero warnings.
* **stb linkage:** the hand-written `extern "C" int stbi_write_png(...)` forward-declaration in `renderer.h` matches the real signature in `third_party/glfw/deps/stb_image_write.h` exactly, including `STBIWDEF` resolving to `extern "C"` under `__cplusplus` — no ODR/linkage risk.

## 3. Residual risk / confidence

Confidence in 1.1 is high but not verified by reproduction — it's the one finding I'd want re-checked with a live run (kwin headless compositor is now available per `5058d21`) before trusting this feature for automated visual validation. 1.2 and 1.3 are low-risk polish items, safe to fix opportunistically.

**Overall:** don't treat this as clean. Route 1.1 back to an implementer before leaning on `--screenshot-out`/F12 for anything that matters (e.g. Phase 1 visual validation) — the fix is small (move one copy encode above `Present()`) but the failure mode if left as-is ranges from silently wrong screenshots to a hard crash in the exact tool meant to catch rendering bugs.

---

## 4. Verification of fix (branch `fix/screenshot-present-ordering`, commit `96f3165`)

**Methodology:** re-read the full diffs line-by-line (`git diff origin/main origin/fix/screenshot-present-ordering -- renderer.h main.cpp`), rebuilt incrementally, and re-ran the same battery of CLI edge cases used in the original audit, at the same rigor.

### 4.1 [1.1 — readback-after-Present] Fixed, verified statically

`renderer.h::RenderFrame()` now, in order: `pass.End()` (line 490) → `PrepareScreenshotReadback(surfaceTexture.texture, *pendingScreenshotPath_, encoder)` which calls `encoder.CopyTextureToBuffer(...)` on the *same* encoder (lines 492–496) → `encoder.Finish()` / `queue_.Submit()` (498–499) → `surface_.Present()` (501). The copy is now recorded and submitted to the GPU strictly before `Present()` is ever called, so it reads the fully-rendered contents of the frame that is *about to be* presented, while the app still legitimately owns the texture. This closes the swapchain-ownership violation from 1.1. `WriteScreenshotPng()` (buffer map + PNG write) correctly stays after `Present()` — it only touches the readback `wgpu::Buffer`, not the swapchain texture, so that ordering is safe.

Checked the failure branch too: if `surface_.Present()` returns non-`Success` (Outdated/Lost/Timeout) on a frame where a copy was already encoded and submitted, the function returns `{}` without calling `WriteScreenshotPng` or resetting `pendingScreenshotPath_` — the already-submitted copy's readback buffer is just dropped (harmless, GPU-side, no leak since it's a refcounted `wgpu::Buffer` local), and the screenshot request stays pending to be retried on the next frame that actually presents. No mis-marking of a screenshot as "done" when it wasn't.

**Still not verified live** — same limitation as the original audit. This container has no display tooling yet: `DISPLAY`/`XDG_RUNTIME_DIR` are unset, `/tmp/.X11-unix` is empty, and `kwin_wayland`/`Xvfb`/`Xwayland` are not installed (the `5058d21` image rebuild hasn't reached this shell). Ran `./build-native/pgu --screenshot-out=/tmp/audit_verify.png --screenshot-after-frames=30` directly: it fails immediately and cleanly at GLFW init (`GLFW Error (65550): Failed to detect any supported platform`), in 4ms — confirming no regression in the no-display path, but this does **not** exercise the actual copy/readback/PNG-write code at all. That code path is still only statically verified, not reproduced. Recommend a real run once the container image is rebuilt with the kwin/xdotool/imagemagick tooling, checking the resulting PNG's dimensions/header (and ideally its content) as the true acceptance bar.

### 4.2 [1.2 — hang risk] Mostly fixed; one narrow residual gap

`FailIfAutoScreenshotStalled()` in `main.cpp` is called every loop iteration (both the minimized-continue path and the normal path), and fires `Fail()` with a clear diagnostic message (presented-frame count, target, pending status) if 10s pass with zero *newly presented* frames while a screenshot is still outstanding (`presentedFrames_ < screenshotAfterFrames || exitAfterPendingScreenshot_`). It correctly resets on every actual presentation, so it won't false-positive on a normal slow-but-eventually-presenting startup — verified by reading: `lastPresentedFrameTime_` updates in the same block that increments `presentedFrames_`, and the watchdog only compares against "time since last successful present," not overall wall-clock time. It's also correctly scoped to auto-capture only (`--screenshot-out`); interactive F12 use is exempt, as it should be.

**Residual gap:** in the minimized branch, the watchdog check runs *after* `glfwWaitEvents()`, which blocks until GLFW sees at least one event on the connection — it has no built-in timeout. In the specific "minimized at startup" scenario the fix explicitly targets, if the windowing/compositor environment produces literally zero events for longer than 10s (plausible in a degenerate headless setup with no periodic compositor activity), the watchdog's check is delayed until whatever event finally arrives, not strictly capped at 10s. This is a real but narrow edge case — it's still a large improvement over the prior unconditional hang (which had no exit at all), just not a hard 10s guarantee in that one sub-case. Not blocking, but worth a follow-up if unattended CI reliability in a truly inert compositor matters (e.g. `glfwWaitEventsTimeout()` instead of `glfwWaitEvents()`).

### 4.3 [1.3 — parseUint32 messaging] Fixed, verified directly

Rebuilt and re-ran the exact repro commands from the original finding:

```
$ ./pgu --screenshot-out=/tmp/x.png --screenshot-after-frames=abc
Invalid value for --screenshot-after-frames: abc
$ ./pgu --screenshot-out=/tmp/x.png --screenshot-after-frames=99999999999999999999
Invalid value for --screenshot-after-frames: 99999999999999999999
$ ./pgu --screenshot-out=/tmp/x.png --screenshot-after-frames=0
Invalid value for --screenshot-after-frames: 0
```

All three now report the intended flag-qualified message instead of the bare `stoull` exception text. Confirmed correct.

### 4.4 Build

Touched both changed files and ran `cmake --build build-native`: clean, zero warnings/errors.

### 4.5 Verdict

**Needs one more narrow look, not a full round.** 1.1 (the serious one) and 1.3 are fixed and hold up under the same line-by-line scrutiny as the original audit — 1.1's actual runtime behavior is still unverified for lack of a display in this container, same caveat as before. 1.2 is substantially fixed; the `glfwWaitEvents()`-has-no-timeout gap in the minimized path is a minor, non-blocking follow-up rather than a reason to reject this fix. Recommend merging, with a live `--screenshot-out` run (real PNG inspection) once display tooling is available, and optionally a follow-up swapping `glfwWaitEvents()` for a timeout-bounded wait in the minimized branch.

---

# Validator Audit Report — Phase 2 Bloom/HDR/Tonemapping Pipeline

**Methodology:** Adversarial-Collaborative Audit, static/code review (overnight autonomous work — director acting on this without waiting for the user)
**Branch reviewed:** `feature/bloom-hdr-tonemap`, commit `8a9f6dc` "Add HDR bloom and tonemapping pipeline" (`renderer.h`, `shaders/postprocess.wgsl`, `CMakeLists.txt`, `cmake/embed_shaders.cmake`), reviewed in an isolated worktree at `/home/eric/Projects/pgu-bloom-audit` (detached at `origin/feature/bloom-hdr-tonemap`, shared checkout untouched)
**Status:** **Clean on correctness, one real design mismatch found (item 4) that should be fixed before Phase 3 depends on this.** No resize/staleness bugs, no shader-math red flags, no build issues.

---

## 1. Resize correctness — verified correct

`CreatePostProcessTextures()` (allocates `hdrSceneTexture_`, `brightPassTexture_`, `blurTempTexture_`, `bloomTexture_` at the current `framebufferWidth_`/`framebufferHeight_`) is called from `ConfigureSurface()`, right after `CreateDepthTexture()` — the exact same choke point the depth buffer already relied on pre-bloom. Traced every path that can reach `ConfigureSurface()`:

* `RenderFrame()`'s `if (framebufferResized_) { surface_.Unconfigure(); ConfigureSurface(); }` — this is what the earlier deferred/settle-then-fallback drag-resize fix ultimately drives (`main.cpp`'s `ReconcileFramebufferSize`/`FramebufferSizeCallback` are unchanged by this branch — no `main.cpp` diff at all — they still just flip `framebufferResized_`, and `RenderFrame()` still reconfigures on that flag before doing anything else this frame).
* The `Outdated`/`Lost` `GetCurrentTexture` status branch — same `Unconfigure()`/`ConfigureSurface()` pair.

Both paths are checked at the very top of `RenderFrame()`, before the star pass or `RunPostProcessPasses()` touch any texture view — so a resize always fully completes (new textures + rebuilt bind groups, see §2) before that frame's postprocess passes are recorded. No stale-sized intermediate texture can get baked into a frame. Also checked the minimized case (`framebufferWidth_/Height_ <= 0`): `CreatePostProcessTextures()` early-returns exactly like `CreateDepthTexture()` already did, and `RenderFrame()` itself bails before reaching any of this — consistent, not a new gap.

## 2. Bind group staleness — verified correct

`CreatePostProcessBindGroups()` is called unconditionally at the end of **both** `CreatePostProcessTextures()` and `CreatePostProcessPipelines()`, guarded by an early-return if either the layouts or the texture views don't exist yet:

```cpp
if (singleTextureBindGroupLayout_ == nullptr || finalCompositeBindGroupLayout_ == nullptr ||
    hdrSceneView_ == nullptr || brightPassView_ == nullptr ||
    blurTempView_ == nullptr || bloomTextureView_ == nullptr) {
    return;
}
```

At startup, `InitDevice()` (calls `ConfigureSurface()` first) runs before `CreateRenderPipelines()` (calls `CreatePostProcessPipelines()` last) — confirmed via `main.cpp:276-280`. So the first `CreatePostProcessBindGroups()` call (from `ConfigureSurface`) bails early (no layouts yet), and the second (from `CreatePostProcessPipelines`) succeeds once both prerequisites exist. On every subsequent resize, only `ConfigureSurface()`/`CreatePostProcessTextures()` runs again — but the layouts already exist from startup and never change, so the guard passes and all four bind groups (`brightExtractBindGroup_`, `blurHorizontalBindGroup_`, `blurVerticalBindGroup_`, `tonemapBindGroup_`) get unconditionally rebuilt against the **new** texture views every time. No stale bind group pointing at a destroyed texture is possible — WebGPU's own ref-counting also means even the brief overlap (old bind group still referencing the old texture object at the moment the new texture is created) is safe, since a `BindGroup` holds its own reference to the resources it binds, independent of the app's own `wgpu::Texture` member being reassigned.

## 3. HDR/tonemap math — checked against standard references, no red flags

* **ACES filmic curve** (`aces_filmic` in `postprocess.wgsl`): constants `a=2.51, b=0.03, c=2.43, d=0.59, e=0.14` are exactly Krzysztof Narkowicz's well-known 2015 ACES approximation — a standard, correct reference implementation, not an ad hoc curve.
* **Bright-pass soft-knee threshold** (`fs_bright_extract`): `threshold=1.0`, `knee=0.35`. Worked through the curve by hand at several luminance values (0.9, 1.0, 1.35, 2.0) — it's monotonic, has no discontinuity at the knee boundaries, ramps in smoothly starting at `threshold - knee` and asymptotically approaches full pass-through as luminance grows, i.e. behaves exactly like the standard "soft threshold" popularized by Jorge Jiménez/Unity's bloom (this is a valid reparameterization of that same idea, not a novel/untested formula). Values aren't wildly off — a 1.0 threshold against non-tonemapped HDR scene colors and a 0.35 knee is a normal, conservative starting point.
* **Composite** (`fs_tonemap`): `bloom_strength = 0.45`, `hdr = scene + bloom * bloom_strength`, then ACES — standard add-back-then-tonemap bloom compositing, correct order (tonemap happens after combining, not before).

No live rendering was possible (see §6), so this is math/formula-level verification only, not a visual confirmation that the bloom "looks right."

## 4. Screenshot interaction — real issue: captures post-ImGui, likely wrong for Phase 3

Traced the full command-recording order in `RenderFrame()`: star pass → `RunPostProcessPasses()` (bright-extract → blur H → blur V → tonemap, writing the final tonemapped image into the swapchain `backbufferView`) → conditional ImGui pass (`LoadOp::Load` on `backbufferView`, draws the tuning panel/FPS overlay on top) → `PrepareScreenshotReadback(surfaceTexture.texture, ...)`. The screenshot copy is recorded **after** the ImGui pass, so `--screenshot-out`/F12 capture the frame **with the ImGui tuning panel and FPS counter baked in** — unchanged from the pre-bloom behavior, just now sitting on top of the tonemapped bloom output instead of the raw un-tonemapped swapchain draw.

**This is very likely wrong for Phase 3's reference-image comparison use case.** Automated pixel-diffing against reference images needs a deterministic, UI-free frame:
* The FPS counter changes every frame — guarantees a diff against any fixed reference image regardless of whether the actual rendering is correct.
* The tuning panel occupies real screen area and can visually overlap the content (stars/bloom) that Phase 3 actually wants to verify.
* Font/AA rendering of ImGui text can vary subtly across systems, adding noise unrelated to the rendering bug being tested.

Recommend: for `--screenshot-out` (the automated/CI path), skip the ImGui render pass entirely for the captured frame (or capture from a texture copy of `backbufferView` taken right after the tonemap pass, before ImGui draws) — giving Phase 3 a clean, deterministic scene-only image. F12 (interactive, manual use) can reasonably keep including the UI overlay, since a human taking a screenshot generally wants "what I'm looking at, panel included." This wasn't a regression introduced by this branch (the ordering choice predates bloom) but bloom is what makes it acutely relevant now, since Phase 3 is the reason `--screenshot-out` exists at all.

## 5. WGSL/Dawn correctness

* **Texture usage flags:** all four HDR intermediates (`hdrSceneTexture_`, `brightPassTexture_`, `blurTempTexture_`, `bloomTexture_`) get `RenderAttachment | TextureBinding` via the shared `CreateHdrTexture()` helper — exactly what each needs (rendered into by one pass, sampled by the next), nothing over- or under-provisioned. No `CopySrc`/`CopyDst`/`StorageBinding` requested anywhere they aren't needed.
* **Bind group layouts:** both `singleTextureBindGroupLayout_` and `finalCompositeBindGroupLayout_` declare `TextureSampleType::UnfilterableFloat`, matching a shader that never declares a `sampler` and only ever calls `textureLoad` — internally consistent, and this is the *correct*, in fact *required*, sample type for a texture-only (no sampler) binding of this shape. No validation mismatch.
* **On the "unfilterable textureLoad for RGBA16Float portability" rationale specifically:** the stated reasoning is shakier than the implementation choice itself. Per the WebGPU format table, `rgba16float` is filterable in core (no `float32-filterable`-style feature gate needed) — only the 32-bit float formats require that. So portability wasn't actually at risk either way. That said, going `UnfilterableFloat`+`textureLoad` **does not hurt this particular blur's quality**: the 5-tap kernel does exact discrete texel fetches at integer offsets regardless of whether the bound sample type is filterable, so every tap lands exactly where the math intends. What it *does* forgo is a possible efficiency win — the classic trick of using a linear-filtered `textureSample` to combine two adjacent taps into one interpolated fetch, roughly halving the sample count for equivalent-looking output. Given this is a full-resolution (non-downsampled), two-pass separable blur running every presented frame, that's a real perf headroom left on the table, not a correctness problem. Worth a follow-up if frame time on this path ever becomes a concern; not blocking.
* **Pass/pipeline structure:** each of the 4 fullscreen passes (bright-extract, blur×2, tonemap) is correctly `Begin`'d and `End`'d sequentially on the same encoder before the next starts (no overlapping-render-pass validation risk); none declare a depth-stencil attachment or vertex buffers, matching `vs_fullscreen`'s builtin-only, buffer-free 3-vertex trick; `colorTarget.blend` is left null (full overwrite) on all of them, consistent with every pass being a full-viewport replace.

## 6. Live verification: still not possible in this container

Checked explicitly, as instructed: `$DISPLAY`, `$WAYLAND_DISPLAY`, `$XDG_RUNTIME_DIR` are all empty; `/tmp/.X11-unix` has no socket; `/run/user/*/wayland-*` doesn't exist; `xdotool`, `wmctrl`, `kwin_wayland`, `Xwayland` are all absent from `PATH`. The `5058d21` headless-tooling image rebuild has not reached this container/worktree. Ran the built binary directly to confirm: it fails immediately and cleanly at GLFW init (`GLFW Error (65550): Failed to detect any supported platform`), same as every prior audit in this environment — no bloom-specific regression in that failure path, but this means **none of §3's shader math or §4's screenshot-ordering conclusion has been visually confirmed** — both are from reading the code and shader math by hand, not from a rendered frame. A real `--screenshot-out` run and PNG inspection (bloom glow visible and not blown out/invisible, no shear, ImGui panel present/absent as expected) remains the strongest outstanding verification step once display tooling is available.

## 7. Build

Fresh `cmake -S . -B build-native` + full `cmake --build build-native -j$(nproc)` in this isolated worktree: clean, zero warnings or errors, all targets (`glfw`, `pgu`) built successfully. `./pgu --help` still works correctly.

## 8. Verdict

**Not clean — one real, actionable finding (§4), everything else checks out.** Resize/bind-group handling (§1–2) is correctly wired end-to-end and should not regress the earlier hard-won drag-resize fix. The bloom/tonemap math (§3) is standard and sane by hand-inspection. The WGSL/Dawn texture and bind group setup (§5) is correct, with one low-priority efficiency nit (foregone bilinear-tap optimization; the stated "portability" rationale for going unfilterable doesn't hold up, but the choice itself is harmless). §4 is the one item I'd actually block on before Phase 3 builds reference-image comparisons on top of `--screenshot-out`: capturing post-ImGui will poison any pixel-diff against a clean reference image. Recommend: merge the bloom pipeline itself, but route a small follow-up to make `--screenshot-out` capture pre-ImGui (or suppress ImGui for that specific captured frame) before Phase 3 depends on it. Live visual confirmation of the bloom output and the screenshot ordering is still outstanding, blocked on display tooling not yet being present in this container.

---

# Validator Audit Report — Screenshot-Pre-ImGui Fix (Lightweight)

**Methodology:** Quick confirmation pass (director pre-diffed and pre-built; scope explicitly small/mechanical)
**Branch reviewed:** `fix/screenshot-pre-imgui`, commit `fcc285e` "Capture screenshots before ImGui overlay" (`renderer.h` only), reviewed in an isolated worktree at `/home/eric/Projects/pgu-ssfix-audit` (detached at `origin/fix/screenshot-pre-imgui`, shared checkout untouched), addressing §4 of the bloom-pipeline audit above.
**Status:** **Clean.**

**Diff scope confirmed:** `git show fcc285e` touches only `renderer.h`, 6 lines added / 6 removed — a pure relocation of the existing `screenshotReadback`/`PrepareScreenshotReadback` block, no logic changes, no other files touched. Matches the director's description exactly.

**1. Ordering, verified by reading the merged `RenderFrame()`:** star pass → `RunPostProcessPasses(encoder, backbufferView)` (writes the final tonemapped scene into the swapchain texture) → `PrepareScreenshotReadback(surfaceTexture.texture, ...)` (now here, encoding `CopyTextureToBuffer` into the same encoder) → ImGui scissor-guard + conditional ImGui pass (`LoadOp::Load`, draws on top) → `encoder.Finish()` / `queue_.Submit()` → `surface_.Present()`. This satisfies both properties at once: the copy is still encoded and submitted strictly before `Present()` (the earlier swapchain-ownership fix stays intact — nothing moved relative to `Present()`, only relative to the ImGui pass), and it now runs after the tonemap write but before the ImGui draw, so it captures the correct texture: final tonemapped scene, clean of UI overlay. Also confirmed the copy command sits validly between two render passes (tonemap pass already `End()`ed, ImGui pass not yet begun) — no overlapping-render-pass hazard from interleaving a copy there.

**2. No broken dependency on ImGui-relative ordering:** the moved block only populates the local `std::optional<ScreenshotReadback> screenshotReadback` — no other side effects. The consuming code (`if (screenshotReadback.has_value()) { WriteScreenshotPng(...); pendingScreenshotPath_.reset(); result.screenshotWritten = true; }`) is unchanged and unmoved, still runs after `Present()` succeeds, and only branches on *whether* a copy was queued this frame, not *when* in the encoder it was recorded — so it's agnostic to the reorder. `exitAfterPendingScreenshot_` (in `main.cpp`, untouched by this diff) keys off `result.screenshotWritten` exactly as before. No logic depends on capture-relative-to-ImGui ordering that could have broken.

**3. Build:** fresh `cmake -S . -B build-native` + full `cmake --build build-native -j$(nproc)` in this isolated worktree — clean, zero warnings/errors.

**Not verified live** — same standing caveat as every prior audit in this environment: `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR` are all empty and `xdotool`/`kwin_wayland` aren't installed here. Static/code review only; a real `--screenshot-out` run (confirming the resulting PNG has no ImGui panel/FPS text baked in) is still the outstanding acceptance step once display tooling exists.

**Verdict: clean, no further changes needed.** This closes bloom-audit finding §4.

---

# Validator Audit Report — Dust Extinction (Phase 2, deep-dive)

**Methodology:** Adversarial-Collaborative Audit, full depth (same rigor as the bloom-pipeline audit), static/code review — overnight autonomous work, director acting on this without waiting for the user
**Branch reviewed:** `feature/dust-extinction`, commit `5162ab2` "Add dust extinction rendering" (`galaxy_model.h`, `galaxy_system.h`, `gpu_types.h`, `main.cpp`, `renderer.h`, `shaders/dust_extinction.wgsl`, `tuning_panel.h`, `CMakeLists.txt`, `cmake/embed_shaders.cmake`; 604 insertions), reviewed in an isolated worktree at `/home/eric/Projects/pgu-dust-audit` (detached at `origin/feature/dust-extinction`, shared checkout untouched)
**Status:** **Clean on everything I could check statically.** No bugs found in any of the 9 focus areas. One genuinely interesting non-bug finding (sorting is correctly implemented but not actually load-bearing given the blend mode — see §2) and one design-scoping judgment call the director asked for directly (§4). This branch has more riding on an actual visual check than any prior audit — see §9.

## 1. LOD tier genuinely separate from the star hierarchy — confirmed independent

Traced every piece of the dust path end-to-end in `galaxy_system.h` and it is a fully parallel structure, not stars-with-dust-mixed-in:

* **Separate leaf generation:** `GenerateDustSamples()` draws a fixed `kDustLeafCount = 24000` dust samples via its own rejection-sampling loop over disk components — reuses `SpiralArmPitchTheta`/`SpiralArmDensityFactor` (the extracted arm-density helpers, see §8) for spatial placement, but with a **deliberately different, documented arm-bias curve**: dust's acceptance probability is `normalized²` vs. stars' plain `normalized` (comment: "dust is more tightly arm-biased than stars") — i.e. this is evidence the implementer actually thought about dust's different density characteristics rather than blindly copying star logic.
* **Separate tree:** `dustHierarchyNodes_`, `dustRootNodeIndex_`, `dustNodeExpandedLastFrame_` are all distinct members from `hierarchyNodes_`/`rootNodeIndex_`/`nodeExpandedLastFrame_`. `BuildDustHierarchyFromSamples()` is its own Morton-sort + k-way merge function, textually separate from `BuildHierarchy()` (the star tree). Both happen to use a 4-way merge fanout (`kMergeFanout = 4`, each declared as an independent local constant in its own function) — that's a shared generic tree-shape parameter, not reused density/LOD tuning.
* **Independently tuned LOD threshold:** `kDustPixelErrorThreshold = 0.14f` (fixed) vs. stars' `pixelErrorThreshold_ = 0.08f` (UI-adjustable) — dust is deliberately coarser, a real, distinct tuning choice, not a shared value. (`kLodHysteresisBand = 0.25f` is shared between `SelectCut`/`SelectDustCut`, but that's a generic anti-flicker band fraction, not a density characteristic.)
* **Separate GPU cap:** `kMaxDustSurfels = 4096` (`gpu_types.h`) vs. `kMaxCutSurfels = 16384` — distinct buffers, distinct cut-count members (`dustSurfelCount_` vs. `cutSurfelCount_`).
* **Separate emission path:** `EmitDustSurfel()` is its own function. It reuses the same EWA-splat screen-space projection math as `EmitSurfel()` — appropriate, since that math (project a 3D Gaussian covariance to a 2D screen ellipse) is generic to *any* Gaussian splat, star or dust cloud; duplicating it isn't a sign of "stars-with-dust-stuffed-in," it's the correct amount of reuse for genuinely shared geometry math.

One minor observation, not a bug: the dust tree repurposes the existing `SurfelNode.brightness` field to hold *accumulated optical depth* (summed across merged children) rather than energy-conserving luminous brightness. Summing optical depth across merged dust clumps is a reasonable coarse-LOD proxy (thin-medium extinction is roughly additive along similar sightlines) but it's a different accumulation semantic riding on a field literally named `brightness` — a one-line comment at the field's dust usage site would save a future reader a double-take.

## 2. Sorting: correctly implemented, but not actually load-bearing for this blend mode

`SortDustCutBackToFront()` (`galaxy_system.h`) sorts descending by `dustSortDepths_[i] = Dot(camForward_, rel)` — a real per-splat camera-forward distance, and descending order is genuinely back-to-front (farthest first, nearest last). It's called inside `BuildCut()` immediately after `SelectDustCut()` and before the caller ever reads `DustPositions()/DustStatic()` for upload (`main.cpp`: `BuildCut()` → `UploadDustCut()` → `RenderFrame()`, every frame, no caching) — so the ordering-and-freshness half of this focus area is clean: real depth, right point in the frame, recomputed every frame, never stale.

**But here's the finding:** I traced the actual blend state (`renderer.h`): color blend is `srcFactor=Zero, dstFactor=Src, op=Add` → `result = dst_old * src_color`. Scalar multiplication is commutative and associative (up to negligible floating-point rounding) — so for *any* two dust splats overlapping the same pixel, `dst * src_A * src_B == dst * src_B * src_A` regardless of draw order. **The final image is provably identical regardless of what order the dust splats are drawn in, under this exact blend equation.** The back-to-front sort is correctly implemented and would matter under an order-dependent blend (e.g. standard alpha-over), but under *this* multiplicative blend it's not actually doing anything for correctness today. Not a bug — it doesn't produce a wrong image, and it's cheap (see below) — but worth knowing, since a future reader could reasonably (and incorrectly) assume the sort is load-bearing and be reluctant to touch it, or conversely assume the blend mode must be order-dependent because a sort exists. Worth a one-line comment noting the sort is currently precautionary/future-proofing rather than required, or removing it if the blend model is expected to stay multiplicative.

**Perf:** `n <= 4096`, `std::sort` with a trivial float comparator — tens of microseconds at most, utterly negligible next to a full-resolution multi-pass GPU pipeline (bloom alone is 4 full-screen passes/frame per the earlier audit). Not a frame-time concern. One small, unrelated nit: `SortDustCutBackToFront()` heap-allocates three new `std::vector`s (`sortedPositions`/`sortedStatic`/`sortedDepths`, up to 4096 elements each) *every frame*, where the surrounding code already has a pattern of pre-sizing persistent member scratch space once (`dustSortDepths_.resize(kMaxDustSurfels)` in the constructor) — reusing pre-sized scratch members instead of fresh per-frame vectors would match that pattern and avoid per-frame heap churn. Low priority, not a correctness issue.

## 3. Blend mode correctness — verified, attenuates bloom's source correctly, no double-counting

Confirmed the exact `wgpu::BlendState` in `renderer.h`: `dustBlend.color = {srcFactor: Zero, dstFactor: Src, op: Add}` (→ `dst' = dst * src`, real multiplicative attenuation — "light passing through dust gets dimmer" is exactly this operation) and `dustBlend.alpha = {srcFactor: Zero, dstFactor: One, op: Add}` (→ alpha passes through unmodified — deliberate, harmless, since none of the downstream postprocess shaders read alpha).

Traced the pass ordering in `RenderFrame()`: star pass (Clear, writes `hdrSceneView_`) → dust pass (`LoadOp::Load` on the *same* `hdrSceneView_`, multiplies in place) → `RunPostProcessPasses()` (bright-extract reads `hdrSceneView_`, ..., tonemap reads `hdrSceneView_` again as the "scene" term). Because the dust pass modifies `hdrSceneView_` in place, **exactly once**, before either downstream consumer reads it, both the bright-pass/bloom contribution *and* the direct "scene" term in the final composite inherit the dust attenuation exactly once each — not zero times (dust does affect bloom), not twice (no double-attenuation). This is a clean, correct design: attenuate the one shared buffer before anything branches off it, and every branch downstream automatically sees the same already-attenuated data.

## 4. Scoped-out star/dust depth interleaving — my honest read: acceptable for now, but flag it, don't silently accept it

The implementation attenuates the *entire accumulated* HDR scene through one multiplicative dust pass, rather than compositing dust against individual stars by depth. Concretely, this means: a star that is genuinely *nearer* to the camera than a dust lane will still be dimmed by that dust lane, when physically it shouldn't be (its light never passes through dust on the way to the camera). That is a real, physically-wrong case, not a hypothetical one.

**Why I think it's an acceptable simplification for this phase:** this is a galaxy-scale board renderer, not a per-star close-up renderer (per project context, the galaxy/surfel view is the game's board). At galaxy-wide viewing distances, real interstellar dust lanes in actual spiral galaxies behave visually almost exactly like this approximation — a comparatively thin foreground screen relative to the enormous depth of the stellar disk behind it, producing the well-known "dark lane across a swath of the image" look in real astrophotography. At that scale, whole-buffer attenuation and true per-star depth compositing would likely be visually indistinguishable.

**Where it stops being acceptable:** the moment the camera gets close to or inside the disk plane, or does an oblique pass across a single arm — situations where a viewer could plausibly see a star clearly nearer than a dust lane and still watch it get dimmed. That's the kind of thing a human notices immediately and reads as "wrong," not "stylized."

**My recommendation:** don't block merging this on it — it's a reasonable, clearly-flagged, bounded choice for Phase 2 — but do explicitly flag it as a known structural limitation for Phase 3, not just a performance corner-cut. Concretely: if Phase 3's reference images are ever generated from a close/oblique camera angle, or from any renderer/tool that does true depth compositing, there will be a *systematic* mismatch near dust lanes that no pixel-diff tolerance will paper over — that's a "the reference methodology needs to account for this," not "the renderer has a bug" situation. Worth a one-line note in whatever Phase 3 planning doc governs reference-image generation, so nobody spends time chasing a "diff" that's actually this known, intentional approximation.

## 5. fBM/value-noise correctness — standard, correct structure

`fbm()` in `dust_extinction.wgsl`: 5 octaves, amplitude halves each octave (`amplitude *= 0.5`, persistence 0.5 — standard default) while frequency roughly doubles (`p * 2.03 + offset` — the `2.03` instead of exactly `2.0` is a common, deliberate trick to avoid octave-to-octave grid alignment artifacts), normalized by the running sum of amplitudes so the output stays in a consistent range regardless of octave count (important since the caller feeds this into a `smoothstep(0.40, 0.88, ...)` threshold). `value_noise()` is standard hash-based bilinear value noise with the classic smootherstep interpolant (`f*f*(3-2f)`). No irregularities — this is a textbook-correct fBM, not a novel/untested formula.

## 6. Resize correctness — no new risk, and mostly not applicable

The dust position/static vertex buffers (`dustPositionBuffer_`, `dustStaticBuffer_`) are fixed-capacity (`kMaxDustSurfels = 4096`), created once in `CreateGeometryBuffers()` (called once at startup from `main.cpp`, alongside `InitDevice()`/`CreateRenderPipelines()` — confirmed, not called from `ConfigureSurface()` or anywhere in the resize path). They are **not sized from `framebufferWidth_`/`framebufferHeight_`** at all — same as the pre-existing star cut buffers always were — so there's no analogous "did resize recreate this at the new size" question to ask; there's no size dependency to begin with.

The one screen-size-dependent resource the dust pass touches is `hdrSceneView_`, which it doesn't own or create — it just draws into the *same* HDR texture the star pass and bloom pipeline already use, and that texture's resize handling was already verified correct in the bloom audit (§1–2 there) and is untouched by this branch. No new resize-staleness risk introduced.

## 7. Per-channel wavelength extinction — correct direction

`DustSample::extinction = {0.42, 0.66, 1.00}` (R, G, B — confirmed this codebase's established channel order from the bloom audit's luminance-weight check) combined with `transmittance = exp(-extinction * optical_depth)` in the fragment shader: since `exp(-x)` decreases as `x` grows, the **largest** extinction coefficient gets the **most** attenuation. Blue (1.00) is attenuated most, red (0.42) least, green (0.66) in between — correctly directional interstellar reddening (shorter wavelengths scattered/absorbed more), matching real extinction curves in direction even though it isn't a photometrically exact CCM/Fitzpatrick curve (which is fine — the ask was directional correctness, not photometric accuracy).

## 8. Scope check: `galaxy_model.h` changes are genuinely just arm-density reuse

Diffed `galaxy_model.h` in full: it's a pure extraction refactor. The exact same `innerRadius`/`spiralB`/`bandWidth`/`band`/`thetaPitch` formula that used to be inline in `GenerateStar()` is now a named function `SpiralArmPitchTheta()`; the exact same `1 + armContrast*cos(armCount*(angle-thetaPitch))` formula is now `SpiralArmDensityFactor()`. Byte-for-byte identical math, just made callable so the dust generator can reuse it. Cross-checked against `docs/stellar_population_framework.md` (taxonomy/color/luminosity population sub-tables) — nothing in this diff touches color, spectral type, luminosity, or population tables at all; it's purely geometric arm-density placement math, orthogonal territory. Scoping claim confirmed, no stealth stellar-population work.

## 9. Live verification: not possible, and this is the audit where that caveat matters most

Checked explicitly, as always: `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR` all empty, no X11/Wayland socket, `xdotool`/`kwin_wayland`/`Xwayland` not installed in this container. Fresh `cmake -S . -B build-native` + full `cmake --build build-native -j$(nproc)`: clean, zero warnings, all targets built; `./pgu --help` works.

**I want to be plain rather than hand-wave confidence here, per the director's ask:** this feature has more riding on an actual rendered image than any prior audit in this repo. Everything above is verified by reading code and shader math by hand — I have **not** visually confirmed: whether the fBM filament pattern actually looks like plausible dust lanes at real scale, whether the `0.035–0.155` optical-depth range times `kDustOpticalDepthScale=0.18` produces a visually subtle-vs-invisible-vs-overwhelming effect, whether the dust splats' Gaussian footprints (`effRadius` from `3σ` of the covariance) look reasonably sized against the arm width at typical camera distances, or whether the whole-buffer attenuation from §4 looks acceptable or visually jarring in practice. All four of those are the kind of thing that can look completely different in a real image than the math suggests on paper. A real `--screenshot-out` run and visual inspection — ideally at a few different camera distances/angles, specifically including one close/oblique pass near a dust lane to sanity-check §4's judgment call — is the strongest, and still entirely outstanding, verification step here.

## 10. Verdict

**Clean — no correctness bugs found in any of the 9 focus areas**, across LOD-tree independence, sort/blend correctness, fBM structure, resize handling, wavelength direction, and scope discipline. Two things worth carrying forward, neither blocking: (a) the back-to-front sort is correct but not currently necessary given the multiplicative blend — cheap enough either way, just worth a comment so nobody's confused about which is true; (b) the whole-buffer depth-interleaving simplification (§4) is a reasonable Phase 2 choice that should be explicitly flagged for Phase 3's reference-image methodology, not silently carried forward as if it were physically exact. The build is clean. The one thing I can't respond to with real confidence is what this actually looks like — that's squarely a "get a real screenshot" item, not a code-review item, and I'd treat this branch as provisionally-approved-pending-a-look rather than fully clean until someone (human or a display-capable agent) actually sees it render.

---

# Validator Audit Report — Emission Nebulae (Phase 2, deep-dive, last item)

**Methodology:** Adversarial-Collaborative Audit, full depth (same rigor as the dust-extinction audit), static/code review — overnight autonomous work, director acting on this without waiting for the user
**Branch reviewed:** `feature/emission-nebulae`, commit `05a0d88` "Add emission nebulae rendering" (`galaxy_system.h`, `gpu_types.h`, `main.cpp`, `renderer.h`, `tuning_panel.h`; 365 insertions; **zero changes to `galaxy_model.h`** and no new shader file — confirmed, see §7), reviewed in an isolated worktree at `/home/eric/Projects/pgu-nebulae-audit` (detached at `origin/feature/emission-nebulae`, shared checkout untouched)
**Status:** **Clean.** No correctness bugs across any of the 8 focus areas. Same standing caveat as the dust audit: this needs a real screenshot before being called fully done.

## 1. LOD tier genuinely separate from both star and dust hierarchies — confirmed independent

`nebulaHierarchyNodes_`/`nebulaRootNodeIndex_`/`nebulaNodeExpandedLastFrame_` are distinct members from both `hierarchyNodes_` (star) and `dustHierarchyNodes_` (dust). `BuildNebulaHierarchy()`/`BuildNebulaHierarchyFromSamples()` is its own Morton-sort + k-way-merge function (4-way fanout, same generic tree shape as star/dust but its own local constant, not a shared/reused merge routine). Sanity-checked the implementer's node-count claim by hand: a 4-ary merge tree over 1800 leaves produces levels of size 1800 → 450 → 113 → 29 → 8 → 2 → 1, summing to exactly **2403** nodes — matches their CPU probe exactly, which is good independent evidence this is a real tree being built, not a fabricated number. Own threshold (`kNebulaPixelErrorThreshold = 0.028`, notably *finer* than dust's 0.14 and even stars' 0.08 — nebula expands to leaf-level detail far more readily, consistent with wanting individually-distinct knots rather than dust's soft merged lanes), own cap (`kMaxNebulaSurfels = 2048`, `gpu_types.h`), own buffers, own emit function (`EmitNebulaSurfel`, reusing the same generic EWA splat-projection math as star/dust emission — appropriate shared geometry, not tree aliasing). This is a genuine third independent tier.

## 2. Placement/arm-density reuse and cubed-bias sanity — confirmed deliberate, not arbitrary

`GenerateNebulaSamples()` calls `SpiralArmPitchTheta(...)` and `SpiralArmDensityFactor(...)` directly (`galaxy_system.h:184,192`) — the exact same shared helpers dust uses, not reimplemented. Combined with the confirmed zero-diff on `galaxy_model.h` (§7), this is real reuse, not incidental similarity.

The acceptance-bias progression across all three tiers is now: stars use `normalizedArm` linearly, dust uses `normalizedArm²` (per the dust audit), nebula uses `normalizedArm³` here. That's a **monotonically increasing concentration toward arm peaks** as tracer type gets more tightly arm-correlated — and that ordering matches real astrophysics: HII regions/emission nebulae are the tightest, most classic visual tracer of spiral structure (young massive stars die close to where they're born), more concentrated than the general dust distribution, which is itself more concentrated than the full (dynamically-relaxed, age-mixed) stellar population. This reads as a coherent, reasoned design across three separate implementer sessions, not a coincidence or an arbitrary pick.

**"Individually distinct crisp knots" plausibility:** 1800 total leaves against a 2048 cap means the cap is *not* the binding constraint here (unlike dust, where 24K leaves >> a 4096 cap forces heavy reliance on merged/coarse nodes) — nebula can render close to every individual leaf as its own splat without cap-driven forced merging; LOD merging only kicks in when a node's projected size actually gets small with distance. 1800 discrete star-forming knots across a spiral galaxy's arms is also a plausible order-of-magnitude count for HII-region-scale features (real spirals like the Milky Way have on the order of hundreds to low-thousands of cataloged significant HII regions). This supports the "individually distinct crisp knots" goal — not too sparse (1800 is a lot of features across several arms) and not effectively continuous (no cap-forced merging into a haze at typical viewing distances).

## 3. No-sorting reasoning — verified correct

`nebulaAdditivePipeline_ = createSplatPipeline(&additiveBlend);` reuses the *exact same* `additiveBlend` `wgpu::BlendState` variable already used for `splatAdditivePipeline_` (stars) — not a separately-constructed lookalike, the literal same object. Confirmed its definition: `srcFactor=One, dstFactor=One, operation=Add` on both color and alpha components → `dst' = dst + src`. Addition is commutative and associative, so accumulating any number of nebula splats over a pixel gives the same result regardless of draw order — exactly the same order-independence property stars have always relied on (stars have never been sorted either; this isn't a new argument, it's the established pattern extended consistently). No sort call exists anywhere in the nebula path (`SelectNebulaCut` doesn't invoke one, and `BuildCut()` doesn't call one for nebula) — matches the no-sorting claim, and the reasoning holds up under direct verification of the actual blend factors, not just by analogy.

## 4. Pass ordering, HDR feed, and the dust-attenuation question — verified correct ordering; honest read on the physics below

**Ordering, confirmed from `renderer.h`:** star pass → dust pass (multiplicative, `LoadOp::Load` on `hdrSceneView_`) → nebula pass (additive, also `LoadOp::Load` on the same `hdrSceneView_`) → `RunPostProcessPasses()` (bright-extract/blur/tonemap). Nebula's contribution is added to `hdrSceneView_` strictly *after* dust's multiplicative pass has already run and strictly *before* the single tonemap pass reads the buffer — so nebula correctly feeds the bloom bright-pass and gets tonemapped exactly once, never missed, never double-tonemapped. Mechanically exactly as claimed.

**The physics, since dust's multiply already ran before nebula adds its light, nebula's own glow is *not* attenuated by dust at all** — it sits fully at its own brightness on top of whatever's left after dust dimmed the stars. The director's question is genuinely good: HII regions really are physically embedded in the same dusty molecular clouds that formed them, so in reality their light absolutely does get extincted by surrounding dust — so is the current "nebula bypasses dust" choice wrong?

**My honest read: it's a defensible, not obviously wrong, choice at this level of approximation — I wouldn't call the alternative "more correct" either.** The reason: the *existing* dust model (already accepted in the prior audit) is a whole-screen-buffer attenuation with no depth/spatial awareness — it doesn't know which specific stars are behind a given dust concentration vs. in front of it. If nebula were also run through that same non-depth-aware multiply, you'd get a *second* application of an already-approximate effect, and the actual visual risk cuts the *other* direction from what you'd naively expect: since dust and nebula are both concentrated in the same disk/arm regions (both use arm-density placement), a nebula knot sitting in the same on-screen area as a dust concentration would get dimmed by dust *regardless of whether that specific gas is actually behind that specific dust in 3D* — quite possibly muddying or washing out the very knots that are supposed to read as bright, crisp features (the doc's explicit goal from §2). Given neither path does true depth compositing, "nebula bypasses dust" is at least as reasonable as "nebula gets multiplied by dust too," and arguably safer for the stated visual goal (crisp, bright, individually-distinct knots) than compounding two independent whole-buffer approximations.

**Recommendation:** treat this the same way as the dust audit's §4 — not a bug, but flag it explicitly as a known, deliberate simplification for Phase 3's methodology (a renderer or reference set that assumes physically-correct dust-occludes-nebula compositing will show a systematic difference here, especially in dense knot-in-dust-lane regions), rather than silently treating it as settled physics. I'd revisit this specific choice only if/when true depth-aware compositing gets built for dust generally — at that point nebula should clearly go through it too, for the reason the director gave.

## 5. Color sanity — reads as pink-red, not orange or over-purple, matches the "natural H-alpha, not Hubble-palette" intent

`color = ClampColor({1.0, 0.26 + 0.12·blueLift, 0.42 + 0.24·blueLift})` for `blueLift ∈ [0,1]`, i.e. `R=1.0` always, `G∈[0.26,0.38]`, `B∈[0.42,0.66]`. Since `B` is consistently higher than `G` (0.42 > 0.26 at minimum, 0.66 > 0.38 at maximum) but never reaches or exceeds `R`, this sits on the pink/rose/magenta-leaning side of red rather than the orange side (orange would need `G > B`) — at `blueLift=0` it's a deeper rose-red, at `blueLift=1` a lighter pink with more blue-white OB-star lift, and it never approaches true magenta/purple since `B` stays well below `R` throughout. That's a good match for "natural-color H-alpha pink-red" as actually seen in real broadband (non-Hubble-palette) nebula astrophotography, and is clearly distinct from a Hubble-palette look (which would involve strong green/teal presence from mapped `[OIII]`/`[SII]`, absent here entirely). `ClampColor` itself is a pre-existing helper from the untouched `galaxy_model.h`, already used for star color tinting — reused, not reinvented.

## 6. Resize correctness — no new risk, same conclusion as dust

`nebulaPositionBuffer_`/`nebulaStaticBuffer_` are fixed-capacity (`kMaxNebulaSurfels = 2048`), created once in `CreateGeometryBuffers()` (confirmed same startup-only call site as the dust buffers, called once from `main.cpp` alongside `InitDevice()`/`CreateRenderPipelines()`, never from `ConfigureSurface()` or anywhere in the resize path). Not sized from `framebufferWidth_`/`framebufferHeight_` at all. The only screen-size-dependent resource the nebula pass touches is `hdrSceneView_`, which it doesn't own — same already-verified resize handling from the bloom audit applies, untouched by this branch. No new resize-staleness risk.

## 7. Scope check — confirmed, zero `galaxy_model.h` changes, no scope creep

`git diff origin/main HEAD -- galaxy_model.h` returns **zero lines** — independently reconfirmed the director's own check. Full file list touched by this branch: `galaxy_system.h`, `gpu_types.h`, `main.cpp`, `renderer.h`, `tuning_panel.h` — no doc files, no `stellar_population_framework.md`, nothing touching color/taxonomy/population tables. Scope fully respected.

## 8. Live verification: not possible, same caveat as dust, said plainly

Checked explicitly: `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR` all empty, no X11/Wayland socket, no `xdotool`/`kwin_wayland`/`Xwayland` in this container. Fresh `cmake -S . -B build-native` + full `cmake --build build-native -j$(nproc)`: clean, zero warnings, all targets built; `./pgu --help` works.

Like dust, this feature has real open questions that only a rendered image can answer: does 1800 knots at the cubed arm-bias actually look like "many individually distinct knots" rather than a handful of blobs or a diffuse haze, does the pink-red color read correctly once alpha-blended additively against the star field and then bloomed/tonemapped (HDR bloom can shift perceived color/saturation in ways hand-tracing the math doesn't capture), and — most interesting given §4 — does "nebula un-attenuated by dust" look fine or does it visibly look like nebula glow floating in front of dust lanes it should arguably be dimmed by. None of that is answerable from code alone. Same recommendation as dust: get a real `--screenshot-out` run and look at it, ideally including a view where a nebula knot and a dust lane visually overlap on screen, to sanity-check §4's judgment call concretely.

## 9. Verdict

**Clean — no correctness bugs found across any of the 8 focus areas.** LOD-tier independence, arm-density reuse, blend/no-sort reasoning, pass ordering, color, resize handling, and scope discipline all check out under direct verification (not just plausibility — node counts were hand-computed and matched, blend states were confirmed to be the literal same object as the already-trusted star pipeline, `galaxy_model.h`'s diff was independently confirmed empty). The one substantive judgment call — whether nebula should be attenuated by dust — has a real, non-obvious answer: I don't think either choice is clearly "more correct" given neither dust nor nebula do true depth compositing, and the implemented choice (nebula bypasses dust) is arguably the safer one for the stated "crisp distinct knots" visual goal. Flag it for Phase 3's methodology, don't block on it. Build is clean. Same standing caveat as dust: this needs a real screenshot before it's fully done, and I'd treat it as provisionally-clean-pending-a-look, consistent with how the dust audit was left. With this being the last Phase 2 item per the director, the natural next step is the full bloom+dust+nebulae integration build/sanity-check on `main` the director mentioned, followed by an actual visual pass across all three features together — that combined view is likely to surface anything the math-level review here can't.

---

# Validator Audit Report — Renderer Code-Health Split (behavioral-equivalence check)

**Methodology:** Adversarial-Collaborative Audit, same bar as the prior App-decomposition audit (`fcfb87e`) — line-by-line comparison of moved code against its pre-refactor original, not just plausibility review. Overnight autonomous work, director acting on this without waiting for the user.
**Branch reviewed:** `refactor/renderer-decomposition`, commit `f07f5d7` "Split renderer maintenance helpers" (`renderer.h` 1372→1032 lines, plus two new files: `renderer_postprocess.h` 244 lines, `renderer_screenshot.h` 165 lines), reviewed in an isolated worktree at `/home/eric/Projects/pgu-split-audit` (detached at `origin/refactor/renderer-decomposition`, shared checkout untouched). Base commit is `64fc270`, the fully-merged bloom+dust+nebulae state.
**Status:** **Clean.** Verified as genuine code motion, not a rewrite — this doesn't need a display environment, and doesn't get one anyway (checked, none available).

## 1. Diff verified as genuine code motion, not rewritten-with-subtle-changes

Extracted the pre-refactor `renderer.h` (`git show 64fc270:renderer.h`, 1372 lines) and compared every moved function against its new location, not by trusting the diff summary but by reading both sides in full:

* **`renderer_postprocess.h`:** `CreatePostProcessPipelines`→`CreatePipelines`, `CreatePostProcessTextures`→`CreateTextures`, `CreateHdrTexture`, `CreatePostProcessBindGroups`→`CreateBindGroups`, `CreateSingleTextureBindGroup`, `RunPostProcessPasses`→`RunPasses`, `DrawFullscreenPass`, and `Shutdown`'s bloom-resource teardown block — every one of these is **byte-for-byte identical body logic** to the original. The only differences are the necessary and expected adaptations for crossing a class boundary: `CreatePipelines`/`CreateTextures` now take `device`/`surfaceFormat`/`framebufferWidth`/`framebufferHeight` as explicit parameters (assigned to the class's own copies) instead of reading `Renderer`'s shared members directly, `CreateFullscreenPipeline`/`CreateHdrTexture`/`CreateSingleTextureBindGroup` gained a `const` qualifier (correct — none of them mutate state, confirmed by re-reading each), and `DrawFullscreenPass` became `static` (correct — it only touches its own parameters, never `this`).
  One nuance worth recording: `CreateTextures`'s `if (framebufferWidth <= 0 || framebufferHeight <= 0) return;` guard checks the **incoming parameters** and returns *before* `framebufferWidth_`/`framebufferHeight_` (`RendererPostProcess`'s own now-separate copies) get assigned — differing from the original, which checked the already-current shared member directly. Traced whether this creates observable staleness: it doesn't — those members are `private` to `RendererPostProcess`, read only synchronously inside the same function right after being assigned, and there's no external getter exposing them. The guard-then-assign vs. assign-then-implicit-guard difference is invisible from outside the class. Verified equivalent by tracing, not just by inspection.
* **`renderer_screenshot.h`:** `PrepareScreenshotReadback`→`PrepareReadback` and `WriteScreenshotPng`→`WritePng` are, likewise, **byte-for-byte identical** — same row-padding/stride math (`AlignTo`, `kScreenshotBytesPerPixel=4`, `kCopyBytesPerRowAlignment=256`), same BGRA↔RGBA channel swap loop, same `MapAsync` + `ProcessEvents`/`Tick`/1ms-sleep polling loop, same `Fail()` messages verbatim (e.g. "Surface does not support CopySrc...", "Cannot capture an empty surface texture", "Screenshot readback map failed", "Unsupported surface format for screenshot capture", "stbi_write_png failed for screenshot path: ..."), same `stbi_write_png` call and console log line. `RendererScreenshotCapture` went further than `RendererPostProcess` on this extraction: it's **fully stateless** — every method is `static`, taking `device`/`instance`/`surfaceFormat`/`surfaceSupportsCopySrc` as explicit parameters rather than holding any of its own. Called as `RendererScreenshotCapture::PrepareReadback(...)`/`::WritePng(...)`, never instantiated — there is no object lifetime question for this class at all.
* **Call-order preservation, the specific thing this audit needed to protect:** traced the full body of the new `RenderFrame()` and confirmed the sequence is unchanged from the just-verified-correct state: star pass → dust pass (`postProcess_.HdrSceneView()` as target, replacing direct `hdrSceneView_` member access — same view, same semantics) → nebula pass → `postProcess_.RunPasses(encoder, backbufferView)` → `RendererScreenshotCapture::PrepareReadback(...)` (still *before* the ImGui pass, still *before* `encoder.Finish()`/`queue_.Submit()`/`Present()` — the screenshot-ordering fix from earlier tonight is fully intact, not regressed) → ImGui pass → `Finish`/`Submit` → `Present()` → (on success) `RendererScreenshotCapture::WritePng(...)`. Every conditional (`dustSurfelCount > 0`, `nebulaSurfelCount > 0`, the ImGui scissor guard, the `Outdated`/`Lost`/`Timeout` surface-status switch) is present, unchanged, in the same order.
* `postProcess_.CreatePipelines(device_, surfaceFormat_)` is called at the same point (end of `CreateRenderPipelines()`) and `postProcess_.CreateTextures(device_, framebufferWidth_, framebufferHeight_)` at the same point (`ConfigureSurface()`, immediately after `CreateDepthTexture()`) as the original — meaning the bloom-audit's already-verified resize-correctness reasoning (every resize path funnels through `ConfigureSurface()`, which recreates the HDR/bright/blur/bloom textures and rebuilds bind groups) transfers over unchanged; this refactor doesn't touch resize logic at all, just where the code that runs during it lives.

## 2. ODR/includes and ownership — no double-ownership, no dangling lifetimes

Both new headers are `#pragma once`-guarded with minimal, correct includes (`renderer_postprocess.h`: `<array>`, `webgpu_cpp.h`, `shaders_generated.h`; `renderer_screenshot.h`: standard headers plus `webgpu_cpp.h` and the same `extern "C" int stbi_write_png(...)` forward-declaration the original used — signature unchanged, so no ODR risk against `third_party/glfw/deps/stb_image_write.h`'s real declaration, already verified compatible in an earlier audit). `renderer.h` includes both new headers immediately after its own project includes — no circular-include risk (neither new header includes `renderer.h` back).

**Ownership:** `Renderer` now holds exactly one `RendererPostProcess postProcess_;` value member — confirmed `hdrSceneTexture_`, `hdrSceneView_`, `brightPassTexture_`/`View_`, `blurTempTexture_`/`View_`, `bloomTexture_`/`View_`, both bind-group layouts, all four postprocess pipelines, and all four postprocess bind groups are **entirely absent** from `Renderer`'s own member list now — they live solely in `RendererPostProcess`, not duplicated. `RendererScreenshotCapture` has no member in `Renderer` at all (stateless, per above) — nothing to double-own. `surfaceSupportsCopySrc_` correctly stays a `Renderer` member (it's a surface-level capability flag, not postprocess- or screenshot-specific) and is passed *by value* into the screenshot functions — no aliasing/reference-lifetime risk. Because `postProcess_` is a plain value member (not a pointer), its lifetime is automatically exactly `Renderer`'s lifetime — not shortened or extended by the refactor. `Renderer::Shutdown()` calls `postProcess_.Shutdown()` at the same point the old inline teardown ran, and its body is (again) byte-for-byte identical to the block it replaced.

## 3. Fresh build — confirmed clean, independently

Ran my own fresh `cmake -S . -B build-native` + full `cmake --build build-native -j$(nproc)` in this isolated worktree (not just trusting the implementer's or director's report): clean, zero warnings, all targets built successfully. No `CMakeLists.txt` changes were needed for the split (both new files are header-only, pulled in via `#include`, no new translation unit to register) — consistent with the diffstat showing only the three `.h` files touched.

## 4. `tests/aspect_ratio_clamp_test.cpp` — verified myself directly, and it was never actually at risk

Compiled and ran it standalone (`g++ -std=c++20 -O2 -I. tests/aspect_ratio_clamp_test.cpp`, no CMake target exists, matching the implementer's claim): **exit 0, "Overall result: SANE (all tests passed)"** across all four width/height cases including the pathological `1×800` and `0×800` ones. Worth noting explicitly: this test only `#include`s `galaxy_system.h` and `vec_math.h` — it never includes `renderer.h`/`renderer_postprocess.h`/`renderer_screenshot.h` at all, and none of those files changed in this commit. So this test wasn't just re-verified passing, it was **never at risk** from this refactor in the first place — a good sanity check to run, but not one that could have caught a `renderer.h`-split regression. (As a nice side effect, its output independently re-confirms the emission-nebulae audit's hand-computed node count again: "leaves=1800 nodes=2403", matching.)

## 5. Line count — confirmed

`wc -l`: `renderer.h` is **1032 lines**, `renderer_postprocess.h` is 244, `renderer_screenshot.h` is 165 — matches the claim exactly, and 1032 is comfortably under the stated 1250-line watchdog limit.

## 6. Display environment

Not applicable and not available (checked anyway for consistency with prior audits: `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR` empty, no `xdotool`/`kwin_wayland`). This audit is pure structural/build verification and doesn't need a rendered frame to be complete — a code-motion refactor with no behavior change has nothing new to see visually that the prior bloom/dust/nebula audits didn't already flag as needing a look.

## 7. Verdict

**Clean.** This is a genuine, careful, behavior-preserving extraction — verified by direct line-by-line comparison against the pre-refactor code, not by pattern-matching or trusting the commit message. The one non-literal adaptation (the `CreateTextures` guard-before-assign reordering) was traced through and confirmed to have no observable effect. No ODR issues, no double ownership, no lifetime risk — the two new classes compose cleanly into `Renderer`, one by value with matching lifetime, one fully stateless with no lifetime to worry about at all. Build verified clean myself. The aspect-ratio test passes but was never actually exposed to this change. Line count confirmed under the watchdog limit. Nothing here needs a screenshot to be called done — this one really is fully clean, not "clean pending a visual check" like the last three.

---

# Validator Audit Report — Color-only pass pipeline attachment fix

**Methodology:** Static, exhaustive pipeline↔pass attachment trace. No GPU driver available in this container, so this is compile-time/logic verification only — I say explicitly below what I can't confirm.
**Branch reviewed:** `fix/postprocess-depth-attachment`, commit `94256b9` "Fix color-only pass pipeline attachments" (1 commit ahead of `main`/`7a3dfe0`), reviewed in an isolated worktree at `/home/eric/Projects/pgu-attach-audit` (detached HEAD — the branch itself was already checked out in the shared main working directory, see §6). Diff: `main.cpp` +1/-1, `renderer.h` +8/-5, `tuning_panel.h` +2/-3.
**Status:** **Clean.**

## 1. Every render pipeline vs. its render pass — traced exhaustively

Went through every `BeginRenderPass`/pipeline pair in `RenderFrame()` (renderer.h) and `RunPasses()` (renderer_postprocess.h):

| Pass | Color format(s) | Depth attachment? | Pipeline used | Pipeline's declared depthStencil | Match? |
|---|---|---|---|---|---|
| Star (scene) pass, L557-593 | `RGBA16Float` (`postProcess_.HdrSceneView()`) | Yes, `depthView_` (`Depth24Plus`) | `hardwarePointPipeline_` or `splatAdditivePipeline_`/`splatAlphaPipeline_` | `Depth24Plus` (`hardwareDepthStencil`/`splatDepthStencil`) | ✅ |
| Dust pass, L595-613 | `RGBA16Float` | No | `dustExtinctionPipeline_` | none (`pipelineDescriptor.depthStencil` never set, L419-429) | ✅ (pre-existing, untouched by this diff) |
| Nebula pass, L615-633 | `RGBA16Float` | No | `nebulaAdditivePipeline_` | none, **as of this fix** (`createSplatPipeline(&additiveBlend, nullptr)`) | ✅ now — was ❌ before (pipeline declared `Depth24Plus`, pass bound no depth texture — this was the crash) |
| Bright-extract / blur-H / blur-V, renderer_postprocess.h L76-78 | `RGBA16Float` each | No | `brightExtractPipeline_`/`blurHorizontalPipeline_`/`blurVerticalPipeline_` | none (`CreateFullscreenPipeline` never sets `depthStencil`) | ✅ (untouched by this diff) |
| Tonemap (composite), L79 | `surfaceFormat_` (matches `backbufferView`) | No | `tonemapPipeline_` | none | ✅ (untouched) |
| ImGui pass, L664-676 | `surfaceFormat_` (`backbufferView`) | No (`imguiPassDescriptor` has no `depthStencilAttachment`) | ImGui WGPU backend's internal pipeline, built from `RenderTargetFormat`/`DepthStencilFormat` passed to `InitImGui` | `RenderTargetFormat = SurfaceFormat()`, `DepthStencilFormat = Undefined`, **as of this fix** | ✅ now — was ❌ before (`DepthStencilFormat` was `renderer_.DepthFormat()` = `Depth24Plus`, but the ImGui pass never bound a depth texture) |

All six color-format matches were already correct before this fix (color target formats were never the issue). `sampleCount` is `1` everywhere — every texture descriptor (`depthDescriptor`, `CreateHdrTexture`, the surface config) and every pipeline's `multisampleState` hard-codes `count = 1`; no MSAA path exists anywhere in this codebase, so there's no sample-count mismatch to check for.

**Confirmed: no second latent mismatch.** The two pairs the fix touched (nebula pipeline↔pass, ImGui pipeline↔pass) are the only two that were ever wrong — every other pipeline/pass pair was already consistent, including the four postprocess passes and the dust pass, none of which this commit touches.

## 2. Nebula depth removal — safe, and the pass never had depth to begin with

Checked whether nebula was ever meaningfully depth-tested against stars before this fix, since removing a depth test silently changing draw order/occlusion would be a real regression. It wasn't, for a stronger reason than "nebula is additive so order doesn't matter": **the nebula render *pass* (`nebulaPassDescriptor`, L621-623) has never bound a depth attachment at all, before or after this commit** — that code is untouched by this diff. Only the *pipeline's* declared `depthStencil` state changed (from `&splatDepthStencil` to `nullptr`). Since Dawn's validation requires a pipeline's depth/stencil declaration to match the bound pass, the pre-fix combination (pipeline says "I have a Depth24Plus attachment", pass says "no depth attachment bound") would have failed pass creation/draw validation immediately — meaning nebula never successfully depth-tested against anything at runtime in the pre-fix code; it just crashed. There is no prior "nebula occluded correctly behind stars via depth test" behavior being removed here, because that behavior never worked in the first place. The fix is purely bringing the pipeline's declaration in line with a pass shape that was already fixed.

Separately, even had the pass carried a depth attachment, `additiveBlend` (`srcFactor=One, dstFactor=One, Add` on both channels) is genuinely commutative/associative, and `splatDepthStencil` — when it *is* used, e.g. for `splatAdditivePipeline_`/`splatAlphaPipeline_` in the star pass — sets `depthWriteEnabled = false`, so even the surviving depth-tested splat pipelines only *read* depth (test against the hardware-point pipeline's writes), never write it themselves. So the "was nebula relying on being depth-tested against stars" question has a firm no on two independent grounds: the pass never bound depth, and the additive math wouldn't have depended on draw order even if it had.

## 3. ImGui depth removal — confirmed correct, no other consumer

`imguiPassDescriptor` (renderer.h L669-671) sets only `colorAttachmentCount = 1`/`colorAttachments = &imguiColorAttachment` — no `depthStencilAttachment` field is populated (it's default/null on `wgpu::RenderPassDescriptor`). So `WGPUTextureFormat_Undefined` is the only correct value for `initInfo.DepthStencilFormat` in `InitImGui`; the pre-fix `renderer_.DepthFormat()` (`Depth24Plus`) was simply wrong for a pass that has never included a depth buffer. Grepped for other `InitImGui`/`DepthFormat()` call sites (`select:Grep` over `*.cpp`/`*.h`): `InitImGui` has exactly one call site (`main.cpp:282`, updated correctly to drop the now-removed parameter), and `DepthFormat()` — the `Renderer` accessor that used to feed it — has no remaining callers at all; it's now dead code (harmless, but worth a follow-up cleanup, not a correctness issue and not something I'd block this fix on).

## 4. Build — clean

Fresh `cmake -S . -B build` + `cmake --build build -j32` in the isolated worktree: configures and links with **exit 0**, no warnings surfaced in the build log. This confirms the fix is at minimum syntactically/type correct; it does **not** confirm the fix resolves the reported Dawn validation crash at runtime — no GPU driver in this container (checked: no `DISPLAY`/`WAYLAND_DISPLAY`, no Xwayland), so the actual "does the app now run past frame 1 instead of crashing" behavior is unverified and needs a real display environment.

## 5. Minimality — confirmed targeted, one pre-existing loose end noted

The diff is exactly the two described changes: `createSplatPipeline`'s lambda gained a second parameter used only to give `nebulaAdditivePipeline_` a `nullptr` depth state (the other two call sites pass `&splatDepthStencil`, unchanged in effect), and `InitImGui`/its one call site dropped the depth-format parameter in favor of a hard-coded `Undefined`. No renaming, no reflow of unrelated lines, no touched files beyond the three needed. The one loose end is the now-dead `Renderer::DepthFormat()` accessor (§3) — leftover from the old plumbing, not cleaned up in this commit. Doesn't affect correctness and I wouldn't hold up the merge for it, but flagging so it doesn't get forgotten.

## 6. Process note (not a fix-correctness issue)

The shared main working directory (`/home/eric/Projects/pgu`) is currently checked out **on `fix/postprocess-depth-attachment` itself**, not `main` — someone worked directly in the shared checkout instead of a worktree. I created my isolated worktree via `git worktree add` as instructed regardless (it came up as a detached `HEAD` at `94256b9` since the branch was already checked out elsewhere), so this audit itself is unaffected, but the shared checkout being off `main` is worth the director's attention independent of this fix's merge.

## 7. Verdict

**Clean, no second latent attachment mismatch.** Both changes are correct and match the pass shapes they bind to; exhaustively traced every pipeline/pass pair in the renderer (scene, dust, nebula, all four postprocess passes, ImGui) and confirmed all are now consistent, with the two that were fixed being the only two that were ever wrong. Nebula's depth removal is safe on two independent grounds (the pass never bound depth at all, and the additive blend is order-independent regardless). ImGui's depth removal is correct given its pass genuinely has no depth attachment, with no other call site depending on the old behavior. Build is clean. Fix is minimal and targeted, modulo one harmless dead accessor left behind. **Can't confirm:** whether this actually eliminates the reported runtime Dawn validation crash — no display/GPU driver in this container to run the app and observe. Recommend merging to `main` on the strength of the static trace (this is unambiguously a real bug being fixed, and the "does it actually stop crashing" question is best answered by whoever next runs it with a real GPU, ideally as the very first thing checked after the next image rebuild). Also flagging the shared-checkout branch state (§6) separately from this fix's own correctness.

---

# Validator Audit Report — kMaxCutSurfels 16384→65536 + bloom re-tune

**Methodology:** Focused static audit on the one part of a tuning change with real correctness stakes (a fixed-capacity constant bump), plus a lower-risk shader-literal sanity check. Reviewed in an isolated worktree at `/home/eric/Projects/pgu-sharp-audit` (detached HEAD — as with the previous audit, the branch was already checked out directly in the shared main working directory rather than a worktree; see the standing note in the prior report).
**Branch reviewed:** `tune/fidelity-sharpness`, commit `8a9c5a2` "Tune galaxy sharpness". Diff confirmed exactly 2 files via `git diff origin/main...8a9c5a2 --stat`: `gpu_types.h` (+1/-1), `shaders/postprocess.wgsl` (+3/-3) — matches the director's description exactly, nothing else snuck in.
**Status:** **Clean.**

## 1. Every use of `kMaxCutSurfels` — all scale off the constant, no hardcoded 16384, no uint16 landmine

`grep -rn "kMaxCutSurfels"` across the whole tree finds exactly these consumers, all of which read the constant rather than a copied literal:

* `gpu_types.h:17` — the definition itself, now `65536`.
* `galaxy_system.h:31-32` — `pointPositions_.resize(kMaxCutSurfels)` / `pointStaticAttributes_.resize(kMaxCutSurfels)` in the `GalaxySystem` constructor: the two CPU-side per-frame cut vectors, sized directly off the constant.
* `galaxy_system.h:740-741` — `SelectCut`'s fixed-capacity guard: `if (cutSurfelCount_ >= kMaxCutSurfels) return;` before any write. Traced `EmitSurfel` (galaxy_system.h:908-922): it writes to `pointPositions_[out]`/`pointStaticAttributes_[out]` where `out = cutSurfelCount_` *before* incrementing — so the guard in `SelectCut` is the only thing standing between this write and an out-of-bounds index, and it correctly gates on the same constant the vectors were sized to. No separate/duplicated bound anywhere.
* `renderer.h:154,159` — `positionBufferDescriptor.size`/`staticBufferDescriptor.size`, the two GPU vertex buffers, also sized directly off the constant (`static_cast<uint64_t>(kMaxCutSurfels) * sizeof(...)`).

Every consumer traces back to the one constant — raising it in `gpu_types.h` keeps all four allocation sites (2 CPU vectors, 2 GPU buffers) and the one guard consistent by construction; there's no fifth place to miss.

**Hardcoded-16384 check:** `grep -rn "16384"` across the project source (excluding `third_party/`) returns **zero hits** — the only `16384` matches anywhere in the tree are inside `third_party/glfw`, `third_party/imgui`'s vendored `stb`/`nuklear` code, completely unrelated constants (a zlib hash table size, Vulkan flag bit values, TrueType font unit scaling). Nothing project-side hardcodes the old cap.

**uint16 check — this was the specific landmine to check for, and it's absent:** `grep -rn "uint16_t\|uint16\b\|u16\b"` across every project header/source/shader/test file returns **zero hits**. Every surfel-related count and index in the codebase — `cutSurfelCount_`, `dustSurfelCount_`, `nebulaSurfelCount_`, the `CutSurfelCount()`/etc. accessors, `SurfelNode::childStart`/`childCount`, every loop index touching the hierarchy or the cut arrays — is `uint32_t` (confirmed by reading the full declaration list in `galaxy_system.h` and `gpu_types.h`, not just grepping the word "count"). `65536` fits `uint32_t` with vast headroom (max ~4.29 billion); there's no off-by-one-at-a-power-of-two risk here because nothing narrower than 32 bits is in the surfel-count path at all.

**No workgroup/dispatch assumption:** grepped for `Dispatch`/`workgroup`/`@compute`/`ComputePipeline` across the whole project tree — zero hits outside `third_party`. This renderer has no compute shaders; the LOD cut is built entirely CPU-side (`SelectCut`, confirmed WebGPU-agnostic per its own header comment) and uploaded as a plain instanced vertex-buffer draw (`pass.Draw(6, cutSurfelCount)` in `renderer.h`'s `RenderFrame`). There is no dispatch grid or workgroup size anywhere that could have been silently tuned for the old 16384 cap.

## 2. Memory/perf sanity — trivial at 4x

`sizeof(PointPosition) == 12` and `sizeof(PointStaticAttributes) == 36` (both `static_assert`-enforced in `gpu_types.h`, so these can't silently drift from the GPU layout). At the new cap:
* GPU position buffer: `65536 × 12 = 786,432` bytes (768 KiB).
* GPU static-attribute buffer: `65536 × 36 = 2,359,296` bytes (2.25 MiB).
* Matching CPU-side vectors: same two sizes again (~3 MiB total CPU + ~3 MiB GPU).

Combined, well under 6 MiB total for both cap-driven allocations doubled across CPU/GPU — four orders of magnitude below any default WebGPU/Dawn buffer-size limit (defaults are in the hundreds-of-MB range; no `requiredLimits` are requested anywhere in `InitDevice` that could clamp this lower). This is not a meaningful allocation by any GPU's standards. Perf-wise, the director's own telemetry (21,792 surfels actually drawn at the test view) is well under even the *old* 16,384 cap's neighborhood at 1.3x — the 65536 ceiling is headroom, not something the renderer will realistically saturate at typical view distances; cost scales with surfels actually emitted (tree-cut traversal cost is bounded by rendered surfels per the class's own doc comment), not the cap.

## 3. Bloom shader literals — confirmed to be the only change, and numerically sane

`git diff origin/main...8a9c5a2 -- shaders/postprocess.wgsl` touches exactly three numeric literals, nothing structural: `fs_bright_extract`'s `threshold` (1.0→1.5) and `knee` (0.35→0.2), and `fs_tonemap`'s `bloom_strength` (0.45→0.22). Traced both functions in full:
* `fs_bright_extract`: `soft = clamp((luminance - threshold + knee) / (2.0 * knee), 0.0, 1.0)` — `knee = 0.2` is still strictly positive, so `2.0 * knee = 0.4` in the denominator is safe, no div-by-zero. `contribution = max(luminance - threshold, 0.0) + soft*soft*knee` and `scale = contribution / max(luminance, 1.0e-4)` — the `1.0e-4` floor on the luminance divisor is untouched by this diff and still guards that division independently of the threshold/knee values.
* `fs_tonemap`: `bloom_strength` is a plain multiplicative scale (`scene + bloom * bloom_strength`) with no division at all — there's no way a smaller positive value here introduces a div-by-zero or numerical instability; it just scales bloom contribution down, consistent with the stated "less haze" intent.

No other lines in the shader changed — confirmed via the stat above (+3/-3, matching exactly the three literal edits, no stray whitespace/logic touched).

## 4. Build — clean

Fresh `cmake -S . -B build` + `cmake --build build -j32` in the isolated worktree: **exit 0**, no warnings surfaced, all targets linked successfully.

## 5. Scope/minimality

Diff is exactly the two described numeric changes across two files — no restructuring, no touched tests (grepped `tests/` for `kMaxCutSurfels`/`16384`/`65536`: no test depends on or asserts against this constant, so none needed updating and none are at risk of now-stale expectations).

## 6. Verdict

**Clean.** The `kMaxCutSurfels` bump is the only part of this change with real correctness stakes, and it checks out completely: every one of its four allocation sites plus the one bounds-check guard scale off the single constant, there is no hardcoded `16384` anywhere in project source, and — the specific landmine the director flagged — **there is no `uint16_t`/narrower-than-32-bit surfel index or count anywhere in the codebase**, so `65536` (which does exceed `uint16_t`'s range by one) has nothing to overflow into. No compute dispatch/workgroup sizing exists at all in this renderer, so that class of assumption doesn't apply here. Memory impact is trivial (~3 MiB CPU + ~3 MiB GPU total, four orders of magnitude under any realistic limit). The bloom shader edits are confirmed to be exactly the three stated literals, all numerically safe (no new div-by-zero, knee/threshold both still positive). Build is clean. Recommend merging to `main`.

---

# Validator Audit Report — nebula/dust cap bumps + leaf counts + shader re-tune

**Methodology:** Focused correctness-only static audit (same class of check as the `kMaxCutSurfels` bump) — the director has already made the aesthetic call from rendered output; this pass verifies only the mechanical/sizing changes are safe. Reviewed in an isolated worktree at `/home/eric/Projects/pgu-fid-audit` (detached HEAD at the branch tip).
**Branch reviewed:** `tune/fidelity-nebula-dust`, commits `3a230dc` ("Tune nebulae as smaller HII knots") and `0ed5bd0` ("Tune dust toward filamentary lanes"). Full diff vs `origin/main` confirmed via `git diff origin/main...HEAD --stat`: exactly 3 files — `galaxy_system.h` (+30/-28... net widened by the diff stat tool's line-splitting, all in the constants/generation functions), `gpu_types.h` (+4/-2, the two cap bumps), `shaders/dust_extinction.wgsl` (+8/-8, the fBM frequency/smoothstep/mix constants). No files outside this set touched.
**Status:** **Clean.**

## 1. `kMaxNebulaSurfels` (2048→4096) and `kMaxDustSurfels` (4096→8192) — every consumer scales off the constant, no narrower type anywhere

`grep -rn "kMaxNebulaSurfels\|kMaxDustSurfels"` (excluding `third_party/`) finds exactly these consumers, none of which copy a literal:

* **Nebula** — `galaxy_system.h:36-37`: `nebulaPositions_.resize(kMaxNebulaSurfels)` / `nebulaStaticAttributes_.resize(kMaxNebulaSurfels)` (2 CPU vectors); `galaxy_system.h:805`: `SelectNebulaCut`'s guard `if (nebulaSurfelCount_ >= kMaxNebulaSurfels) return;`; `renderer.h:175,180`: the two GPU buffer descriptor sizes. Traced `EmitNebulaSurfel` (galaxy_system.h:1022 onward): writes to `nebulaPositions_[out]`/`nebulaStaticAttributes_[out]` at `out = nebulaSurfelCount_` before incrementing — same safe pattern as the already-verified star path, gated by the one guard.
* **Dust** — `galaxy_system.h:33-35`: `dustPositions_.resize(kMaxDustSurfels)` / `dustStaticAttributes_.resize(kMaxDustSurfels)` / **`dustSortDepths_.resize(kMaxDustSurfels)`** (3 CPU vectors — dust has one more array than nebula/stars, for its depth-sort field; confirmed it's sized off the same constant, not a separately-tracked capacity); `galaxy_system.h:775`: `SelectDustCut`'s guard; `renderer.h:165,170`: the two GPU buffer descriptor sizes. Traced `EmitDustSurfel` (galaxy_system.h:964 onward): writes `dustPositions_[out]`/`dustStaticAttributes_[out]`/`dustSortDepths_[out]` all at the same `out = dustSurfelCount_`, then increments — all three arrays move in lockstep off the one guard and the one constant.

**No hardcoded old values:** `grep -rn "\b2048\b\|\b4096\b\|\b8192\b"` across the whole project tree (excluding `third_party/`) returns **only the two new constant definitions in `gpu_types.h` itself** — nothing else in the tree references `2048`, `4096`, or `8192` as a bare literal. (Note `kMaxNebulaSurfels` is now `4096` — the *same* numeral the old `kMaxDustSurfels` used to be — so this grep needed care to confirm both are the new intended constant assignments and not a leftover cross-reference; confirmed by reading the actual lines, both are exactly `gpu_types.h:18-19`, the two definitions.)

**Type consistency, the specific ask:** re-ran the uint16 grep (`uint16_t\|uint16\b\|u16\b`) across the whole project source — still **zero hits**, unchanged from the prior audit. Every surfel count/index in both the dust and nebula paths (`dustSurfelCount_`, `nebulaSurfelCount_`, their accessors, the `EmitDustSurfel`/`EmitNebulaSurfel` `out` locals, the hierarchy node `childStart`/`childCount` fields they share with the star path) is `uint32_t`, exactly like `kMaxCutSurfels`'s handling. `4096` and `8192` both fit comfortably in `uint16_t` too (unlike `65536`), so there was never actually a boundary-crossing risk for these two specifically — but the type consistency is real and confirmed: nothing here uses a narrower type than the star path did, so if a future bump ever does cross 65535 for dust or nebula, the same headroom that made the star bump safe already applies.

**Memory, confirmed trivial:** using the already-`static_assert`-fixed sizes (`PointPosition`=12B, `PointStaticAttributes`=36B):
* Nebula: `4096 × (12+36) = 196,608` bytes (192 KiB) total across the CPU+GPU pair.
* Dust: `8192 × (12+36) = 393,216` bytes (384 KiB) GPU-side; CPU-side adds `dustSortDepths_`'s `8192 × 4 = 32,768` bytes (32 KiB) on top.

Combined new total across both bumps: well under 1 MiB — negligible, consistent with the `kMaxCutSurfels` finding.

## 2. Leaf counts vs. caps — `kNebulaLeafCount` (1800→3200) and `kDustLeafCount` (24000→36000): hierarchy build is leaf-count-agnostic to the cap, confirmed by tracing the actual build/traversal split

Nebula's new leaf count (3200) sits under its new cap (4096), so there's no leafCount>cap case there. Dust's new leaf count (36000) sits well **above** its new cap (8192) — this is the case the director specifically flagged, and it's safe for a structural reason, not a coincidence: **the hierarchy-build functions (`BuildDustHierarchyFromSamples`/`BuildNebulaHierarchyFromSamples`) take their `leafCount` from `dust.size()`/`nebulae.size()` and have no reference to `kMaxDustSurfels`/`kMaxNebulaSurfels` anywhere in their bodies** — traced both in full: they Morton-sort all leaves, then bottom-up-merge in fixed groups of 4 (`kMergeFanout`) until exactly one root remains, building a complete tree over however many leaves were generated, with zero dependency on the render-time cap. The cap only enters at the opposite end — the top-down `SelectDustCut`/`SelectNebulaCut` traversal, gated purely by the count guard (§1) and the projected-size-vs-threshold LOD test. So a 36000-leaf tree feeding an 8192-surfel cap behaves exactly like the already-established star case (60000 leaves against the *pre-bump* 16384 cap, which this same codebase has run with before this whole tuning arc started): the traversal simply stops expanding/emitting once the cap is hit, which can only manifest as *coarser LOD than the view ideally wants* if the view ever demands more detail than the cap allows — a visual-quality question, correctly out of scope for this correctness audit, never a crash or overflow. Confirmed no separate cap-vs-leafCount assertion or assumption exists anywhere that this ordering could violate.

## 3. Other tuning constants — sanity-checked, all sane

Read every changed numeric constant and traced its use:

* **Nebula generation** (`galaxy_system.h` inside `GenerateNebulaSamples`): `coreSigma = (0.018 + 0.062·sizeDraw²) × max(0.8, scaleRadius/3.2)` — `sizeDraw ∈ [0,1]` (from the existing `random01`), so `sizeDraw² ∈ [0,1]`, coreSigma strictly positive, range `[0.018, 0.08]×factor` (previously `[0.05, 0.16]×factor` — smaller as intended for "smaller HII knots", still positive). `verticalSigma = max(0.012, thickness·0.08)` — floor keeps it positive regardless of `thickness`. The second covariance axis scale `(0.55 + 0.75·rand)` and `brightness = (0.45+1.45·rand)·(0.35+0.65·normalizedArm)` are both strictly positive over `rand, normalizedArm ∈ [0,1]` (the latter already `std::clamp`-ed earlier in the same function, unchanged by this diff). `kNebulaAmplitudeScale` (1.15e-5, was 2.4e-5) is a positive multiplier in `EmitNebulaSurfel`'s `amplitude = kNebulaAmplitudeScale · brightness / (2π·sqrtDet)` — it's in the numerator, not the divisor, so a smaller value can't introduce a division problem; the divisor `sqrtDet` has its own pre-existing `1.0e-14f` floor via `e1`/`e2`, untouched by this diff. `kNebulaPixelErrorThreshold` (0.020, was 0.028) stays strictly positive — it's compared against, never divided by.
* **Dust generation** (`GenerateDustSamples`): `longSigma`/`shortSigma`/`verticalSigma` all follow the identical positive-by-construction pattern as nebula's (positive base + positive random term, or a `max()` floor) — just smaller coefficients for thinner lanes, still strictly positive. `opticalDepth = (0.036+0.14·rand)·(0.30+0.85·normalizedArm)` strictly positive. `kDustOpticalDepthScale` (0.24, was 0.18) is a positive multiplier feeding `attr.amplitude = std::min(brightness·kDustOpticalDepthScale, 2.5f)` — the `std::min` ceiling (untouched) still caps it, no overflow risk from the larger scale. `kDustPixelErrorThreshold` (0.09, was 0.14) stays positive, compared-against only.
* **`shaders/dust_extinction.wgsl`**: the fBM frequency multipliers (`11.0`, `36.0`, was `7.5`/`23.0`) only rescale the *input* to `fbm()` — traced `fbm()`'s body: its `norm` accumulator is a fixed geometric sum over exactly 5 fixed-amplitude octaves (`0.5, 0.25, 0.125, 0.0625, 0.03125`, summing to `0.96875`), entirely independent of the caller's frequency scaling, so `sum / norm` can never divide by zero regardless of what frequency the caller passes — confirmed this is unconditionally safe, not just safe for these two specific values. The `smoothstep(0.50, 0.82, ridge_noise)` edges (was `0.40, 0.88`) still satisfy `edge0 < edge1`, required for `smoothstep` to behave (and even if reversed, WGSL's `smoothstep` doesn't trap — but the ordering is correct here regardless). The final blend coefficients (`0.35+0.65·fine_noise`, `0.16+2.05·filament`) are positive-by-construction sums, and `transmittance = exp(-extinction·optical_depth)` has no division at all — a larger `optical_depth` just makes `exp` approach 0 (fully opaque), never NaN/Inf from below.

## 4. Build — clean

Fresh `cmake -S . -B build` + `cmake --build build -j32`: **exit 0**, no warnings, all targets linked.

## 5. Scope

Confirmed via `git diff origin/main...HEAD --stat`: exactly the 3 files described (`galaxy_system.h`, `gpu_types.h`, `shaders/dust_extinction.wgsl`) — no test files reference any of the changed constants (`kMaxNebulaSurfels`/`kMaxDustSurfels`/`kNebulaLeafCount`/`kDustLeafCount`/the old numeric literals), so nothing is at risk of a stale assumption elsewhere.

## 6. Verdict

**Clean.** Both raised caps (nebula 2048→4096, dust 4096→8192) have every consumer — CPU vector resizes (2 for nebula, 3 for dust including the dust-only sort-depths array), the per-path bounds guard, and the GPU buffer descriptor sizes — scaling off the single constant each, with no hardcoded old value anywhere in project source and no narrower-than-`uint32_t` type in either path (consistent with how `kMaxCutSurfels=65536` was already handled; these two specifically never crossed the `uint16_t` boundary anyway, but the type discipline holds regardless). The dust `leafCount(36000) > cap(8192)` case — the one flagged as needing explicit confirmation — is handled safely because the hierarchy-build step has no dependency on the render cap at all; the cap only gates the top-down cut-selection guard, exactly mirroring the pre-existing star path's already-proven pattern. All other changed tuning constants (nebula/dust generation coefficients, dust shader fBM frequencies and smoothstep edges) were traced individually and are sane: strictly positive where required, no new division introduced, `fbm()`'s normalization is frequency-independent so no div-by-zero risk from the frequency changes. Build is clean. Recommend merging as a fidelity checkpoint.
