Adapt vllm-ascend to upstream vLLM changes for step {step_id}.
Previous step: {previous_step_id}
Previous step summary: {previous_step_summary_path}

You are a single agent performing the full {mode} workflow end-to-end.
Do NOT use TeamCreate or Agent tools — work directly without sub-agents.

━━━ REPOSITORIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  vllm:        {vllm_path}
  vllm-ascend: {ascend_path}

━━━ INPUTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  mode:           {mode}
  current step:   {step_id} / is last step: {is_last_step}
  release tag:    {release_tag}
  patch:          {patch_path}
  changed files:  {changed_files_path}
  error logs:     {error_logs}
  archive dir:    {step_dir}

━━━ CUMULATIVE STEP MODEL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The vllm-ascend working tree already contains all successful adaptations from
previous steps. Before making changes:
  1. Read {previous_step_summary_path} if it exists
  2. Reuse prior guards, helpers, imports, and patterns
  3. Avoid reverting prior adaptations unless the current change proves them obsolete

step_summary.md must be cumulative: preserve previous content, append new
"Step {step_id}" section. The step_target.patch is cumulative (git diff HEAD).

━━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Only modify vllm-ascend at {ascend_path} (never vLLM at {vllm_path})
  - Do not run git add, git commit, git reset, or git checkout in vllm-ascend
  - Use vllm_version_is("{release_tag}") for version boundaries — never hasattr/try-except
  - All branches of a version guard must have identical function signatures
  - Static analysis only: do not import vllm/vllm-ascend, run tests, launch models,
    check devices, or require NPU/GPU/runtime dependencies
  - Do not treat ModuleNotFoundError, missing NPU/GPU, or missing runtime
    dependencies from local commands as adaptation failures
  - Never read raw CI logs into context — use structured error_logs from {error_logs}
  - If is_last_step is True, check whether code-structure-guide.md is stale after
    cumulative vllm-ascend changes. If so, write updated version as
    {step_dir}/{code_structure_guide_file}. Do NOT modify the original file.

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Archive to {step_dir}/:
  analysis.md       — subsystems touched, changes, affected files, version guard assessment
  review.md         — static review verdict, guard/signature/import checks, remaining risks
  step_summary.md   — cumulative summary (preserve prior + append Step {step_id})

After completing all work, stop — no extra summary output is required.

━━━ REFERENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{reference_content}
