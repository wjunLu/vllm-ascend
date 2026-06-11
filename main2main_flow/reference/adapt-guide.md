# Adapt Guide

Use this guide during the adapt phase of each main2main step. The goal is not
to copy upstream vLLM changes into vllm-ascend. The goal is to understand which
upstream contracts changed, then update the Ascend implementation that depends
on those contracts.

This file is only about adaptation decisions and code changes. Mechanical
pipeline work, such as updating the pinned vLLM commit reference, generating
patches, running pre_ci_check, running e2e tests, and committing changes, is
handled externally by the main2main flow.

---

## Re-orient (every step, not just the first)

Re-read this file at the start of every step. For code-structure routing, use
`reference/code-structure-guide.md` only when you need to map changed upstream
paths/symbols to likely vllm-ascend files.

Before starting, confirm:
- Current step and upstream range from the prompt
- Compatible release tag from the prompt — needed for any `vllm_version_is()` guards
- The prompt-provided paths for `changed_files_path`, `patch_path`, and `step_dir`
- The previous step summary path, if present, so this step can reuse prior work

---

## Cumulative step model

Each step runs on the same vllm-ascend working tree. Successful changes from
previous steps are already present. Do not reinitialize, revert, or duplicate
those changes. Read the previous step summary path from the prompt when it
exists, then reuse prior version guards, helper functions, imports, and
adaptation patterns.

The per-step `step_target.patch` is generated externally from `git diff HEAD` and
is cumulative from the original vllm-ascend base HEAD through the current step.
Do not run git add, git commit, git reset, or git checkout in vllm-ascend.

---

## Inputs

For each step, use the prompt-provided paths:

- `changed_files_path` / `changed_files.txt` — file paths changed by the upstream step
- `patch_path` / `upstream.patch` — full upstream diff for the step
- `step_dir` — archive directory for `analysis.md`, `review.md`, and `step_summary.md`

Read `changed_files.txt` first. It is a cheap routing signal that tells you
which parts of `upstream.patch` deserve attention.

---

## Step 1: Analyze vLLM Changes

1. Read `changed_files.txt`.
2. When the changed upstream paths/symbols are not obvious, consult
   `reference/code-structure-guide.md` to identify key areas and likely
   vllm-ascend locations. Do not read the whole structure guide unless needed.
3. Find the relevant chunks in `upstream.patch` and identify the concrete change:
   new/removed abstract methods, changed signatures, renamed config fields, moved
   imports, changed constructor args, dependency bumps, or changed return types.
4. Use the structure guide's File Mapping Table to find likely vllm-ascend
   locations that need adaptation.

The key question: **does vllm-ascend subclass, override, call, import, or read
anything this patch changed?** Internal implementation changes only need
adaptation when vllm-ascend directly depends on the behavior.

---

## Step 2: Adapt vLLM Ascend Project

For each related change in vLLM, evaluate whether adaptation in vLLM Ascend is
needed:

- **Internal Architecture Changes**
  Check internal interfaces of vLLM core modules (scheduler, executor, model runner, etc.)
  Update vLLM Ascend's Ascend-specific implementations (e.g., NPU worker/model runner,
  custom attention, custom ops)
  Preserve vLLM Ascend specific modifications (e.g., code under vllm_ascend/)

- **Dependency Changes**
  Check for dependency version changes in pyproject.toml or setup.py, but do not
  blindly mirror upstream vLLM dependency bumps. Only update vLLM Ascend
  dependency declarations when the change is required by vllm-ascend code or by
  the external validation flow.

- **Version Compatibility**

  Every signature change, config field move, or import path change is a potential
  version boundary. Use a guard only when vllm-ascend must support both the
  release API and the new upstream API in the same codebase, and the affected
  code path can run against both versions.

  ```
  Does this vllm-ascend code path need to support both release and upstream main?
    ├─ YES, and the API differs → wrap behavior with vllm_version_is("<release_tag>")
    └─ NO, or an enclosing guard already separates behavior → no new guard needed
  ```

  No guard is needed when the upstream change is internal and vllm-ascend does
  not call, override, import, or read it. When unsure, search existing patterns in
  source and follow the same import style, version string, and branching
  structure. All branches of a version guard must keep identical public function
  signatures.

When a feature genuinely can't be supported on Ascend yet, add a stub with a
`# TODO` comment referencing the issue.

A no-op adapt (nothing to change) is fine, but still write `analysis.md`,
`review.md`, and `step_summary.md` explaining why no vllm-ascend code change was
needed. The main2main flow will still run pre_ci_check and `_run_e2e_test`
externally.

---

## Step 3: Static Self Review

Do not run pre_ci_check.py, tests, imports, model launches, or runtime validation
manually. The main2main flow runs pre_ci_check automatically after each opencode
attempt, and `_run_e2e_test` handles real validation later.

During this AI step, only do static review:
- Inspect the vllm-ascend diff and relevant source files
- Verify version guards use the release tag from the prompt
- Verify guarded branches keep identical public function signatures
- Verify imports by reading source, not by importing vllm/vllm-ascend locally
- Record findings in `analysis.md`, `review.md`, and `step_summary.md`

For adapt mode, `analysis.md` should include:
- Upstream files changed and relevant upstream contracts identified
- vllm-ascend files checked through the File Mapping Table
- `Checked but unchanged` notes for relevant vllm-ascend files that did not need
  edits, with the reason they were unaffected
- Adaptation plan and implemented changes, or no-op rationale
- Version guard decisions and release tag used

For adapt mode, `review.md` should include:
- Static diff review result
- Guard, signature, import, and config-access checks
- Remaining risks or explicit "no known issues"

---

## Code structure routing reference

The vLLM key areas, vllm-ascend key file locations, and File Mapping Table live in
`reference/code-structure-guide.md`.

Use that file as an on-demand routing reference when changed upstream paths or
symbols need mapping to vllm-ascend code. It is intentionally separate from this
workflow guide because it describes relatively stable code structure and can be
refreshed independently.
