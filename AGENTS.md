# AGENTS.md

CrewAI Flow that automates vllm-ascend's main2main upgrade against upstream vLLM. Drives an external `opencode run` subprocess as the AI adapter; everything else is deterministic Python.

## Run

Install once: `pip install -e .`

Real entrypoint is `Main2MainFlow` in `src/flow.py`. Run `python main.py` from repo root:

```bash
python main.py --vllm-path <path|url> --vllm-ascend-path <path|url> [--target-commit SHA]
```

Both repos must be real git checkouts (or HTTPS URLs that will be cloned into `workspace/repos/`). vllm HEAD is the implicit target unless `--target-commit` is given.

## Layout (only the non-obvious bits)

- `src/flow.py` — the Flow; node order: `initialize → analyze_commit_and_plan_step → process_steps → generate_final_post → push_to_github`. Routing uses string signals defined in `utils.py` (`HasCommit`, `HasNoCommit`, `UpgradeCompleted`, `UpgradeFailed`) — match them exactly.
- `src/scripts/` — deterministic helpers (`detect_commits`, `plan_steps`, `update_commit_reference`, `pre_ci_check`, `run_tests`, `push_to_github`). Import them, don't shell out.
- `src/agent/opencode_adapter.py` — spawns `opencode run --format json --dangerously-skip-permissions`, streams JSONL, 30 min total / 5 min stale timeouts, up to 3 stale retries with a continue-prompt.
- `src/agent/prompt.md` — single-agent prompt (do NOT use TeamCreate/Agent sub-tools); formatted with `str.format_map`, so any literal `{}` in this file must be escaped as `{{ }}`.
- `src/reference/` — adapt/diagnosis guides consumed by the agent. Update these when new error patterns appear; they are the durable knowledge base.
- `docs/guide.md` — authoritative long-form spec. When in doubt about behavior, trust `flow.py` + `utils.py` over README; `docs/guide.md` is mostly accurate but predates some filename renames (e.g. it mentions `step_summary.json`; current code uses `step_summary.md`, see `utils.EACH_STEP_SUMMARY_FILE`).

## workspace/ is volatile

`initialize` **deletes and recreates** `workspace/` on every run. Never put anything there you want to keep. All step artifacts (`workspace/steps/<step-id>/upstream.patch`, `step_summary.md`, `step_target.patch`, `opencode.log`, `opencode_raw.jsonl`, `tests/round-*-summary.json`, `pre_ci_check.json`) live under it. Filenames are centralised as constants in `utils.py` — reuse them, don't hardcode strings.

## State & path constants

- `WORKSPACE_DIR = <repo>/workspace` (computed from `__file__`, not cwd).
- Path resolution priority in `initialize`: CLI arg → env var (`VLLM_PATH`, `VLLM_ASCEND_PATH`, `VLLM_TARGET_COMMIT`) → default. URLs starting with `http(s)://` or `git@` are auto-cloned; existing target dirs get **removed first**.
- `initialize` records `original_vllm_ref` / `original_ascend_ref` and `generate_final_post` checks them back out (`-f` for ascend). If you add new checkouts/branch switches mid-flow, make sure restoration still works.

## Retry & test loop semantics

`process_steps`: per step, run `_ai_analysis` then `_run_e2e_test`. Pass → next step, reset `retry_count`. Fail → `retry_count++` and re-enter `_ai_analysis` in fix mode. At `retry_count >= 3` the entire flow short-circuits to `UpgradeFailed` — there is no per-step skip.

Inside `_ai_analysis`, opencode itself is also called up to 3 times per step, gated by `pre_ci_check.run_check`. The first opencode attempt in a step runs in `adapt` mode; subsequent ones run in `fix` mode with `error_logs` populated from `pre_ci_check.json` or the previous round's `round-N-summary.json`.

## Env flags worth knowing

| Var | Effect |
|---|---|
| `SKIP_AI_ANALYSIS=true` | Bypass opencode entirely; only deterministic ops run. Useful for debugging the Flow plumbing. |
| `SKIP_E2E_TEST=true` | `_run_e2e_test` returns True without touching NPU. |
| `PUSH_TO_GITHUB=true` + `GITHUB_REPO=owner/name` | Enables `push_to_github`; requires `gh` logged in. |
| `MAIN2MAIN_REMOTE_HOST`, `MAIN2MAIN_REMOTE_CONTAINER` | `run_tests.py` with `remote="env"` ssh+docker-exec into this host/container. Mac dev boxes have no NPU — always set these or use `SKIP_E2E_TEST=true`. |

## Conventions

- Python 3.10–3.13. Uses `uv` (`uv.lock` is gitignored but present locally). The `.venv/` at repo root is the uv venv — don't recreate.
- No lint/typecheck/test commands are wired up. `tests/` is empty. Don't invent a `pytest` invocation; verify changes by `SKIP_E2E_TEST=true SKIP_AI_ANALYSIS=true kickoff ...` against a small synthetic commit range.
- All adapter outputs that need persistence go through `utils.py` filename constants; introducing a new artifact means adding a constant there first.
- vllm-ascend version guards must use `vllm_version_is("{release_tag}")` exactly — `pre_ci_check` will reject any new `vllm_version_is(...)` call whose tag doesn't match `state.release_tag`.

## Don'ts

- Run `python main.py` from repo root (it adds `src/` to sys.path automatically).
- Don't keep `workspace/` paths between runs; they vanish on `initialize`.
- Don't add `{var}` placeholders to `prompt.md` that aren't passed into `run_opencode_adapter`'s inputs dict, or `format_map` will KeyError.
- Don't commit anything under `workspace/`, `output/`, `.venv/`, or `.env` (already gitignored).
