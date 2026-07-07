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
