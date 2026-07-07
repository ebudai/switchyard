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
