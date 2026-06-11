# Diagnosis Guide

Use this guide during fix mode. The goal is not to rerun validation locally; it
is to read the structured `error_logs` provided by the prompt, trace each
actionable failure back to the upstream change that caused it, and update
vllm-ascend statically. Never modify the vLLM repository; it is only an upstream
reference.

Runtime validation is external. The main2main flow runs pre_ci_check after each
opencode attempt and runs `_run_e2e_test` after `_ai_analysis` completes.

---

## Cumulative fix model

Fix mode runs on the same cumulative vllm-ascend working tree as adapt mode.
Successful changes from previous steps and earlier attempts are already present.
Do not reinitialize, revert, or duplicate them unless the structured error proves
they are obsolete or harmful.

Before changing code:
- Read the previous step summary path from the prompt when it exists
- Inspect existing vllm-ascend changes relevant to the failure
- Reuse prior version guards, helper functions, imports, and adaptation patterns

The patch is generated externally from `git diff HEAD`, so do not run git add,
git commit, git reset, or git checkout in vllm-ascend.

---

## Step 1: Read structured error_logs

Fix mode receives `error_logs` from the prompt. Each entry is a structured JSON
file path produced by the main2main flow. Start from these files; do not read raw
CI logs unless a structured summary explicitly points to a small relevant section.

Possible inputs:

1. `pre_ci_check.json`
   - Produced automatically after an opencode attempt when static checks fail
   - This fix attempt is still before e2e; fix the static policy issue first and
     do not reason about runtime behavior unless the JSON explicitly includes it
   - Currently checks newly added `vllm_version_is()` calls for the expected
     release tag and checks for temporary/debug artifacts in the repo
   - It does not prove that every necessary guard exists, nor that signatures are
     semantically correct; those still require static self-review
   - Fix by reading the JSON and inspecting source; do not rerun pre_ci_check
     manually

2. `tests/round-<N>-summary.json`
   - Produced by `_run_e2e_test` after runtime validation fails
   - The exact schema may vary; prefer fields named `code_bugs` and `env_flakes`
     when present
   - If those fields are absent, inspect the available top-level keys and use the
     most structured error list or summary field before considering raw logs
   - Only actionable code bugs require code changes

If a summary contains only environment flakes or missing local/runtime
dependencies, record that in `analysis.md` and `step_summary.md`; do not add code
workarounds.

---

## Step 2: Classify failures

For each structured error, decide whether it is actionable:

- `code_bugs` → fix in vllm-ascend
- `env_flakes` → no code fix; record in the analysis
- pre_ci static issues → fix statically in vllm-ascend
- local environment errors from commands attempted during the AI adaptation step,
  such as `ModuleNotFoundError: No module named 'vllm'`, missing NPU/GPU, or
  missing runtime dependencies → not an adaptation failure
- similar errors from `_run_e2e_test` structured summaries → classify according
  to the summary; they are usually environment/setup issues, not code fixes

Common code-bug mechanisms:

- `TypeError` → signature change, added/removed/renamed parameter, constructor
  argument change
- `AttributeError` / `KeyError` → config field moved/renamed, new required field,
  changed data shape
- `ImportError` → module path changed or symbol removed
- `NotImplementedError` / abstract class instantiation error → new required
  interface method
- Downstream errors such as `KeyError: 'choices'` → read upward in the structured
  traceback context to find the original engine/model failure. Do not fix
  wrapper/downstream symptoms directly unless they are the first actionable root
  cause.

Then look up the matching pattern in `reference/error-pattern-examples.md`.

---

## Step 3: Correlate with the upstream patch

For each actionable issue:

1. Extract a stable search term from the error message or traceback, such as a
   method name, config field, import path, class name, or keyword argument.
2. Search the current step's `upstream.patch` from the prompt-provided
   `patch_path`.
3. Identify the upstream intent: rename, removal, new parameter, new abstract
   method, new required config, moved module, or changed return type.
4. Map the upstream change to the vllm-ascend code that subclasses, overrides,
   calls, imports, or reads the changed contract.
5. Decide whether a `vllm_version_is("<release_tag>")` guard is required. Use the
   release tag from the prompt.

Do not infer fixes only from symptoms. Prefer root-cause fixes tied to the
upstream diff.

---

## Step 4: Apply fixes statically

Apply the smallest vllm-ascend change that restores compatibility:

- Add missing parameters with safe defaults when overriding upstream methods
- Keep all version-guarded branches' public function signatures identical
- Update config field access through guarded branches or helper methods
- Update imports with guarded import branches when both old and new paths must be
  supported
- Implement new required platform/interface methods with Ascend-appropriate
  behavior
- Remove obsolete usages only when the upstream patch proves the API is gone

Do not run tests, import vllm/vllm-ascend, launch models, inspect devices, or run
pre_ci_check manually. Those checks happen outside the AI step.

---

## Step 5: Write analysis, review, and cumulative summary

Write fix diagnosis into `{step_dir}/analysis.md`. For fix mode, include:

- Structured error source file(s)
- Classification: code bug, env flake, pre_ci static issue, or non-actionable
  local/runtime dependency issue
- Root-cause upstream change and affected vLLM symbol/file
- Affected vllm-ascend file(s)
- Fix plan and implemented fix
- Version guard decision and release tag used

Write `{step_dir}/review.md` with static review results:

- Diff reviewed
- Version guards checked
- Function signatures checked
- Imports/config accesses checked
- Remaining risks or no known issues

Update `{step_dir}/step_summary.md` cumulatively. Preserve previous sections and
append/update the current step section, including a "Carry forward to next step"
subsection.

---

## Stop conditions controlled externally

The main2main flow controls retry limits and validation. Do not try to rerun CI
or override the retry policy manually.

During this AI step, stop after:
- Applying the static fix, or determining there is no actionable code fix
- Writing `analysis.md`, `review.md`, and `step_summary.md`

The next pre_ci/e2e round will be triggered by the main2main flow.

---

## Context management

Structured logs can still be large. Prefer concise evidence:

- Read structured summaries first
- Use targeted source searches for symbols from the error
- Avoid raw CI logs unless the structured summary points to a specific small
  section
- Use `analysis.md` and cumulative `step_summary.md` as external memory instead
  of reconstructing prior decisions from context
