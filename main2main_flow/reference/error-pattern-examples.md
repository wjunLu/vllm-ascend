# Common Error Patterns Reference

These are the most frequently seen failure patterns when upstream vLLM evolves.
Use this reference when diagnosing CI failures or applying fixes.

---

## Method Signature Change

**Error:** `TypeError: forward_oot() got an unexpected keyword argument 'X'` or
`missing 1 required positional argument: 'X'`

**Cause:** vLLM changed a method signature — parameter added, removed, renamed,
or full API replacement (e.g., `disable_full` to `valid_modes`/`invalid_modes`).

**Fix:** Compare signatures at good vs bad commit, then adapt:

```python
from vllm_ascend.utils import vllm_version_is

# Option 1: Add parameter conditionally to call site
kwargs = {"existing_param": value}
if not vllm_version_is("<release_tag>"):
    kwargs["new_param"] = new_value
function(**kwargs)

# Option 2: Add default parameter to OOT method signature
def forward_oot(self, query, key, value, cu_seqlens=None, max_seqlen=None, new_param=None):
    ...
```

For full API replacements, adapt the call site to match the new API — do NOT
blindly add the old parameter.

**Important:** When creating version-guarded branches, all branches must define
the function with identical signatures (convert lambdas to `def` if needed).
Mismatched signatures across branches cause mypy `[call-arg]` errors.

---

## Config/Attribute Change

**Error:** `AttributeError: 'CompilationConfig' object has no attribute 'X'`,
`KeyError: 'field_name'`, or `Config object has no attribute 'Y'`

**Cause:** Upstream moved an attribute/config field between classes, restructured
a config class, or added a new required field (e.g., `bs_to_padded_graph_size`
moved to `CudagraphDispatcher`, `uses_mrope` moved from target to draft model
config, `enable_eplb` added to `FusedMoEParallelConfig`).

**Fix:** Use `vllm_version_is()` to access from the correct location:

```python
if vllm_version_is('<release_tag>'):
    value = self.vllm_config.old_location.attribute
else:
    value = self.new_class.new_location.attribute
```

For config access that changes frequently, consider helper methods like
`_get_positions()` / `_set_positions()` to abstract the logic. For new required
fields, add them to the config wrapper.

---

## Constructor / Dataclass Field Change

**Error:** `TypeError: __init__() got an unexpected keyword argument 'X'`,
`missing 1 required positional argument: 'X'`, or dataclass/config construction
failures.

**Cause:** vLLM changed constructor parameters, dataclass fields, metadata
objects, request/sequence structures, or config wrapper requirements.

**Fix:** Compare the upstream constructor or dataclass definition in
`upstream.patch` with the vllm-ascend call site. Add, remove, or rename fields at
the call site. Use `vllm_version_is("<release_tag>")` only when the same
vllm-ascend code path must support both old and new constructor shapes. Prefer a
small helper when the same field mapping appears in multiple places.

---

## Registry / Plugin Interface Change

**Error:** backend/platform/model loader not found, registry key mismatch,
plugin factory `TypeError`, or an object returned from a registry no longer
matches the expected protocol.

**Cause:** vLLM changed a registry key, plugin constructor, backend factory,
platform registration contract, attention backend interface, or model loader
protocol. vllm-ascend often integrates through these extension points.

**Fix:** Find the changed registry or factory contract in `upstream.patch`, then
update the corresponding vllm-ascend registration, key, constructor arguments, or
returned object. Do not add broad fallback registration unless both old and new
APIs must be supported in the same code path; use a narrow version guard when
needed.

---

## Custom Op Not Registered

**Error:** `AttributeError: '_OpNamespace' '_C' object has no attribute 'op_name'`

**Cause:** vLLM code references `torch.ops._C.op_name` — a CUDA custom op not
available on Ascend.

**Fix:** Register an equivalent Ascend op, or override config to use a different
code path (e.g., re-force `+rms_norm` in `custom_ops` for SP).

---

## Method Return Type Change

**Error:** `TypeError: '>' not supported between instances of 'NoneType' and
'NoneType'` or similar comparison errors on None.

**Cause:** Upstream changed a method from returning `None` to returning a value
(e.g., `float`), and the caller now uses it.

**Fix:** Update the OOT override to return the expected value.

---

## Module Reorganization

**Error:** `ImportError: cannot import name 'X' from 'vllm.old.path'`, or
`error: Cannot find implementation or library stub for module named "vllm.X" [import-not-found]`

**Cause:** vLLM moved/renamed a module, or removed it entirely (e.g., `vllm._bc_linter`).

**Fix:** For moved/renamed modules, use `vllm_version_is()` to branch imports:

```python
if vllm_version_is("<release_tag>"):
    from vllm.old.path import X
else:
    from vllm.new.path import X
```

Follow the existing import style in the file. If module-level guarded imports
create cycles or heavy side effects, move the import to the narrowest local scope.

For removed modules, delete the import **and** all usages (decorators, function
calls) — clean removal over `# type: ignore`.

---

## Platform Interface Addition

**Error:** `TypeError: Can't instantiate abstract class AscendPlatform with abstract method X`

**Cause:** New abstract method added to vLLM's `Platform` base class.

**Fix:** Implement the method in `vllm_ascend/platform.py`. Check the base class
signature and return type, then provide an Ascend-appropriate implementation.

---

## pre_ci_check Failure

**Error source:** `{step_dir}/pre_ci_check.json`

**Cause:** Static policy failure detected by the main2main flow after an opencode
attempt. pre_ci_check currently checks newly added `vllm_version_is()` calls for
the expected release tag and checks for temporary/debug artifacts in the repo.

It does not prove that every necessary guard exists, nor that signatures are
semantically correct. Those still require static self-review.

**Fix:** Read the structured JSON and inspect the affected source files. Apply a
static code fix, then update `analysis.md`, `review.md`, and `step_summary.md`.
Do not rerun pre_ci_check manually; the main2main flow will run it after the AI
step.

---

## Local Environment Missing Dependencies (NO FIX NEEDED)

**Error:** `ModuleNotFoundError: No module named 'vllm'`, missing `vllm_ascend`,
missing NPU/GPU, missing torch-npu/CANN/runtime libraries, or device discovery
failures from local commands attempted during the AI adaptation step.

**Cause:** The AI adaptation environment may contain source code only. Runtime
imports and device checks are not meaningful during the AI step.

**Fix:** Do not add dependency hacks, fallback imports, or code workarounds for
local environment failures. Use static source inspection only. Runtime validation
is handled later by the main2main flow. If a similar error appears in structured
`_run_e2e_test` output, classify it according to that summary; it is usually an
environment/setup issue rather than an adaptation code bug.

---

## Environment Flakes (NO FIX NEEDED)

These are transient infrastructure issues — note them in the report but require
no code changes:

- `OSError: [Errno 116] Stale file handle` — multi-process NFS race
- `ConnectionResetError` — transient network failure
- `filelock` errors — model download contention
- `ConnectionRefusedError` — service not ready
- `TimeoutError` — env flake only when the structured summary classifies it as
  environmental or the context clearly shows infrastructure/resource delay. If
  it follows scheduler, worker, model runner, or code-path changes, treat it as a
  possible code bug.
- `torch.cuda.OutOfMemoryError` — resource exhaustion
- `OSError: No space left on device` — disk full
