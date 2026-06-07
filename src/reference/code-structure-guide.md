# Code Structure Guide

Use this guide as a stable routing reference when mapping upstream vLLM changes
to likely vllm-ascend files. These tables describe code structure, not workflow
policy. Read only the sections needed for the current step.

This file may need refreshing when vllm-ascend structure changes. On the final
main2main step, check whether the vllm-ascend files/directories or mappings below
became stale during the upgrade. If they changed, update this file to match the
current vllm-ascend structure.

---

## vLLM Key Areas to Focus On

When analyzing vLLM changes, pay special attention to these areas that typically
require vLLM Ascend adaptation:

<!-- BEGIN REFERENCE: key-areas -->
1. **Platform Interface** (`vllm/platforms/`)
   - New abstract methods — implement immediately; missing ones cause `TypeError: Can't
     instantiate abstract class AscendPlatform` at runtime, not at import time, so they
     won't surface until a test actually executes
   - Method signature changes
   - New platform capability flags

2. **Worker / Model Runner** (`vllm/v1/worker/`, `vllm/v1/worker/gpu/model_runner.py`)
   - New or removed parameters in `execute_model` or `load_model` — vllm-ascend heavily
     overrides these; signature mismatches cause `TypeError` during inference
   - New lifecycle methods
   - Changes to model runner initialization

3. **Attention** (`vllm/model_executor/layers/attention/`, `vllm/v1/attention/`)
   - New parameters in `forward()` — vllm-ascend registers its own backend; interface
     changes require updating both registration and implementation
   - Changes to attention backend interface
   - MLA-specific updates

4. **MoE** (`vllm/model_executor/layers/fused_moe/`)
   - FusedMoE layer signature changes — vllm-ascend has Ascend-specific MoE kernels
     that call into this interface
   - Router interface changes
   - Activation function changes

5. **Config** (`vllm/config*.py`)
   - Field renames or moves between config classes — vllm-ascend reads config fields
     directly in many places; a rename causes `AttributeError` everywhere it's accessed
   - New required fields
   - Constructor changes

6. **Distributed** (`vllm/distributed/`)
   - Changes to collective op interfaces
   - KV transfer protocol changes
   - Device communicator updates

7. **Speculative Decoding** (`vllm/v1/worker/gpu/spec_decode/`, `vllm/config/speculative.py`)
   - Import path changes
   - Config field changes
   - New proposer interface methods — vllm-ascend has MTP and Eagle proposer implementations

8. **Compilation** (`vllm/compilation/`)
   - Pass manager interface changes
   - New required passes
   - Changes to how passes register

9. **Quantization** (`vllm/model_executor/layers/quantization/`)
   - Quantization config changes
   - compress-tensor method changes

10. **Models** (`vllm/model_executor/models/`)
    - Changes to model forward signatures — when vllm-ascend overrides a model's
      forward method, signature changes break inference
    - New model architectures
<!-- END REFERENCE: key-areas -->

---

## vllm-ascend Key File Locations

<!-- BEGIN REFERENCE: file-locations -->
| Project | Path |
|---------|------|
| vLLM Ascend version compatibility | `vllm-ascend/docs/source/conf.py` |
| vLLM Ascend source code | `vllm_ascend/` |
| **Core Modules** | |
| Ascend-specific attention | `vllm_ascend/attention/` |
| Ascend-specific executor | `vllm_ascend/worker/` |
| Ascend-specific ops | `vllm_ascend/ops/` |
| Scheduling extensions | `vllm_ascend/core/` |
| Device abstractions | `vllm_ascend/device/` |
| Device memory allocator | `vllm_ascend/device_allocator/` |
| Custom CANN ops (placeholder dir) | `vllm_ascend/_cann_ops_custom/` |
| **Specialized Implementations** | |
| Ascend 310P specific | `vllm_ascend/_310p/` |
| EPLB load balancing | `vllm_ascend/eplb/` |
| XLite compiler | `vllm_ascend/xlite/` |
| **Compilation & Fusion** | |
| Graph fusion pass manager | `vllm_ascend/compilation/` |
| Compilation passes | `vllm_ascend/compilation/passes/` |
| **Quantization** | |
| Quantization methods | `vllm_ascend/quantization/` |
| ModelSlim integration | `vllm_ascend/quantization/modelslim_config.py` |
| **Distributed & KV Cache** | |
| KV transfer | `vllm_ascend/distributed/kv_transfer/` |
| Device communicators | `vllm_ascend/distributed/device_communicators/` |
| KV cache offload (CPU/NPU) | `vllm_ascend/kv_offload/` |
| **Speculative Decoding** | |
| Eagle proposer (also dispatches MTP via `method="mtp"`) | `vllm_ascend/spec_decode/eagle_proposer.py` |
| **Sampling & LoRA** | |
| Sampler, rejection sampler, penalties | `vllm_ascend/sample/` |
| LoRA NPU ops | `vllm_ascend/lora/` |
| **Plugin & Model Loading** | |
| Upstream patches (platform / worker) | `vllm_ascend/patch/` |
| Custom model loader | `vllm_ascend/model_loader/` |
| **Profiling** | |
| Torch NPU profiler wrapper | `vllm_ascend/profiler/` |
| **Utility Modules** | |
| Common utilities | `vllm_ascend/utils.py` |
| Ascend config | `vllm_ascend/ascend_config.py` |
| Environment variables | `vllm_ascend/envs.py` |
<!-- END REFERENCE: file-locations -->

---

## File Mapping Table

Use this table after identifying a changed upstream symbol. It points to likely
vllm-ascend locations, not guaranteed locations.

<!-- BEGIN REFERENCE: file-mapping -->
| vLLM upstream path | vllm-ascend path | What to check |
|:---|:---|:---|
| `vllm/platforms/` | `vllm_ascend/platform.py` | Abstract methods, platform capabilities |
| `vllm/v1/worker/` | `vllm_ascend/worker/` | Worker lifecycle, model loading, `execute_model` |
| `vllm/v1/worker/gpu/model_runner.py` | `vllm_ascend/worker/model_runner_v1.py`, `vllm_ascend/worker/v2/model_runner.py` | Runner initialization and execution |
| `vllm/v1/attention/` | `vllm_ascend/attention/` | Backend interface and metadata |
| `vllm/model_executor/layers/attention/` | `vllm_ascend/attention/`, `vllm_ascend/ops/mm_encoder_attention.py` | Attention wrappers and kernels |
| `vllm/model_executor/layers/fused_moe/` | `vllm_ascend/ops/fused_moe/` | MoE kernel interface, router, experts |
| `vllm/distributed/` | `vllm_ascend/distributed/` | Collective ops, TP/PP, KV transfer |
| `vllm/config*.py` | `vllm_ascend/ascend_config.py`, plus call sites under `vllm_ascend/` | Config fields and constructor args |
| `vllm/compilation/` | `vllm_ascend/compilation/` | Passes, fusion rules, registration |
| `vllm/model_executor/models/` | `vllm_ascend/models/` | Model forward signatures and loaders |
| `vllm/model_executor/layers/quantization/` | `vllm_ascend/quantization/` | Quantization methods and kernels |
| `vllm/model_executor/layers/layernorm.py` | `vllm_ascend/ops/layernorm.py` | LayerNorm op interface |
| `vllm/model_executor/custom_op.py` | `vllm_ascend/ops/` | Custom op registration |
| `vllm/v1/worker/gpu/spec_decode/` | `vllm_ascend/spec_decode/` | MTP/Eagle proposer interfaces |
| `vllm/lora/` | `vllm_ascend/lora/` | LoRA op interface, punica integration |
| `vllm/v1/sample/`, `vllm/model_executor/layers/sampler.py` | `vllm_ascend/sample/` | Sampler, rejection sampler, penalty kernels |
| `vllm/model_executor/model_loader/` | `vllm_ascend/model_loader/` | Model loading hooks, weight format adapters |
| `requirements*`, `constraints*`, `pyproject.toml`, `setup.py`, `setup.cfg` | Matching dependency files in vllm-ascend | Dependency versions |
<!-- END REFERENCE: file-mapping -->
