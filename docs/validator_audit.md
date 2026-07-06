# Validator Audit Report — Part 2 Gaussian Refinement

**Methodology:** Adversarial-Collaborative Audit
**Branch Reviewed:** `part2-gaussian/covariance-lod` (worktree: `/home/budai/Projects/pgu-gaussian-audit`)
**Status:** Clean (All checklist items verified, math checked, no defects found)

---

## Math Verification: EWA Projection and `clip.w` Offset Pass-Through

### 1. Verification of the Projection Jacobian
- **Analysis:** Perspective projection maps view-space positions $(t_x, t_y, t_z)$ to NDC coordinates:
  $$x_{ndc} = -f_{aspect} \frac{t_x}{t_z}, \quad y_{ndc} = -f \frac{t_y}{t_z}$$
  Taking the partial derivatives with respect to the view-space coordinates (with $t_z < 0$):
  - $\frac{\partial x_{ndc}}{\partial t_x} = -\frac{f_{aspect}}{t_z}$ (in code: `j00 = -projFOverAspect_ * invTz;` — Matches)
  - $\frac{\partial x_{ndc}}{\partial t_z} = \frac{f_{aspect} t_x}{t_z^2}$ (in code: `j02 = projFOverAspect_ * tx * invTz * invTz;` — Matches)
  - $\frac{\partial y_{ndc}}{\partial t_y} = -\frac{f}{t_z}$ (in code: `j11 = -projF_ * invTz;` — Matches)
  - $\frac{\partial y_{ndc}}{\partial t_z} = \frac{f t_y}{t_z^2}$ (in code: `j12 = projF_ * ty * invTz * invTz;` — Matches)
  - All cross-terms ($\frac{\partial x_{ndc}}{\partial t_y}$ and $\frac{\partial y_{ndc}}{\partial t_x}$) are 0.
- **Conclusion:** The Jacobian matrix construction in `EmitSurfel` matches the standard 3D perspective projection derivatives exactly.

### 2. Matrix Multiplication $\Sigma_{2D} = J \cdot \Sigma_{view} \cdot J^T$
- **Analysis:** By expanding the matrix multiplication for symmetric $\Sigma_{view}$:
  - $\Sigma_{2D, 00} = J_{00}^2 \Sigma_{xx} + 2 J_{00} J_{02} \Sigma_{xz} + J_{02}^2 \Sigma_{zz}$
    - Code: `m0x * j00 + m0z * j02` $\equiv (j_{00} \Sigma_{xx} + j_{02} \Sigma_{xz}) j_{00} + (j_{00} \Sigma_{xz} + j_{02} \Sigma_{zz}) j_{02}$ (Matches)
  - $\Sigma_{2D, 01} = J_{00} J_{11} \Sigma_{xy} + J_{00} J_{12} \Sigma_{yz} + J_{02} J_{11} \Sigma_{xz} + J_{02} J_{12} \Sigma_{zz}$
    - Code: `m0y * j11 + m0z * j12` $\equiv (j_{00} \Sigma_{xy} + j_{02} \Sigma_{yz}) j_{11} + (j_{00} \Sigma_{xz} + j_{02} \Sigma_{zz}) j_{12}$ (Matches)
  - $\Sigma_{2D, 11} = J_{11}^2 \Sigma_{yy} + 2 J_{11} J_{12} \Sigma_{yz} + J_{12}^2 \Sigma_{zz}$
    - Code: `m1y * j11 + m1z * j12` $\equiv (j_{11} \Sigma_{yy} + j_{12} \Sigma_{yz}) j_{11} + (j_{11} \Sigma_{yz} + j_{12} \Sigma_{zz}) j_{12}$ (Matches)
- **Conclusion:** The 2D covariance computation matches the mathematical EWA projection formula.

### 3. Hand-Traced Distance Scaling Check
- **Analysis:** Assuming an isotropic local covariance $\Sigma = \sigma^2 I$ centered at $t_x = t_y = 0$, the projected covariance becomes diagonal:
  $$\Sigma_{2D} = \begin{pmatrix}
  (f_{aspect} / t_z)^2 \sigma^2 & 0 \\
  0 & (f / t_z)^2 \sigma^2
  \end{pmatrix}$$
  The eigenvalues are $e_1 = (f_{aspect} / t_z)^2 \sigma^2$ and $e_2 = (f / t_z)^2 \sigma^2$.
  The half-extents scale with $\sqrt{e_1} \propto 1/|t_z|$ and $\sqrt{e_2} \propto 1/|t_z|$.
  When depth $t_z$ doubles, the major/minor axes halving properties hold.
- **Conclusion:** Screen space size exhibits the correct distance-based scaling.

### 4. Shader Pass-Through Validation
- **Analysis:** The vertex shader computes:
  $$\vec{x}_{clip} = \vec{x}_{center\_clip} + \vec{offset} \cdot clip.w$$
  When dividing by `clip.w` ($w$) to compute NDC:
  $$\vec{x}_{ndc} = \frac{\vec{x}_{clip}}{w} = \vec{x}_{center\_ndc} + \vec{offset}$$
  Since the $\vec{offset}$ (major/minor axis vectors) was already computed in NDC units on the CPU, multiplying by `clip.w` in the vertex shader is correct and mathematically cancels the automatic GPU perspective divide.
- **Conclusion:** Unlike the original Part 1 bug (where the offset had no distance dependence), the offset here is dynamically derived from the perspective projection Jacobian. The `clip.w` multiplication is mathematically correct and does **not** reintroduce the bug.

---

## Findings & Evidence for Checklist Items

### 5. Mixture-of-Gaussians Combination
- **Finding:** The bottom-up covariance moment-matching merge formula is correct.
- **Evidence:**
  - In `BuildHierarchy`, the parent covariance is updated using a loop over its children at [main.cpp:L1168-1173](file:///home/budai/Projects/pgu-gaussian-audit/main.cpp#L1168-1173).
  - The implementation: `cov.xx += w * (n.cov.xx + d.x * d.x)` computes the sum of the child covariance plus the translation term $\vec{d}\vec{d}^T$ weighted by $w_i = b_i / b_{parent}$, matching the moment matching mixture rule perfectly.

### 6. Amplitude Derivation
- **Finding:** The peak amplitude is derived correctly.
- **Evidence:**
  - Peak amplitude uses the closed-form eigenvalues $e_1, e_2$ to compute $\sqrt{\text{det} \, \Sigma_{2D}}$: `sqrtDet = std::sqrt(e1 * e2)`. This correctly determines the determinant of the 2D covariance.
  - The amplitude calculation uses `amplitude = kSplatAmplitudeScale * node.brightness / (2.0f * 3.14159265f * sqrtDet)`, matching the analytical 2D Gaussian integral formula $\text{amplitude} = \frac{\text{brightness}}{2\pi \sqrt{\text{det} \, \Sigma_{2D}}}$.

### 7. Energy Conservation and Disk Flatness Verification
- **Finding:** Luminous flux and physical flatness are conserved and emerge correctly from the tree.
- **Evidence:**
  - Building and running the application outputs:
    ```
    [hierarchy] leaves=60000 nodes=80002 levels: 60000 15000 3750 938 235 59 15 4 1
    [hierarchy] brightness root=90292.8 sum(leaves)=90292.8 sum(root.children)=90292.8 rel.err(root vs leaves)=6.09495e-08
    [hierarchy] root cov diag=(7.28709, 5.13432, 0.0918704) effRadius=8.13004 largestEig=7.34417
    ```
  - The relative error is $\approx 6.1 \times 10^{-8}$ (conveys double/float precision consistency).
  - The root covariance diagonal ($7.29, 5.13, 0.092$) shows that the Z-variance is 1.2% of the X-variance, reflecting the flatness of the disk structure without orientation heuristics.

### 8. Leaf Isotropy
- **Finding:** Level 0 nodes are initialized as isotropic Gaussians.
- **Evidence:**
  - In `BuildHierarchy`, leaf nodes use `IsotropicCovariance(s.radius)` which initializes the covariance as a diagonal matrix with diagonal values $\sigma^2 = s.radius^2$ and off-diagonals as 0.

### 9. Cut-Selection size metric
- **Finding:** Projected size uses `node.effRadius` derived from the true largest eigenvalue, cached at build time.
- **Evidence:**
  - `effRadius` is precalculated during `BuildHierarchy` using `3.0f * std::sqrt(LargestEigenvalue3x3(cov))` and stored in `SurfelNode`. Traversal in `SelectCut` reads this cached value directly.

### 10. Verification of Dead Knobs
- **Finding:** The controls `pointRadiusMultiplier` and `blendStrength` have no effect in the Gaussian shader.
- **Evidence:**
  - The splat shader `kSplatShader` has no references to `camera.point_radius_multiplier` or `camera.blend_strength`, though they remain in the CameraUniforms struct definition to maintain layout compatibility.

### 11. File Scope Compliance
- **Finding:** Only `main.cpp` and `docs/implementer-report.md` were touched.
- **Evidence:**
  - Confirmed via `git diff --name-only origin/main HEAD`.

### 12. Build Validation
- **Finding:** Compilation is clean with zero warnings under `-Wall -Wextra -Wpedantic`.
- **Evidence:**
  - Verified by fresh compilation.

---

## Open Questions

- *None.* The implementation is mathematically robust, clean, and resolves the hierarchical splat representation exactly as intended.
