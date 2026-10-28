# Resolution Center benchmark

Learning project (not production): AI dispute adjudication for INR and SNAD cases.
Overall goal: compare Kev-4B and Qwen-4B (separately and combined) on synthetic cases
before building the full workflow.

**Current phase: Kev only.** Do not install, download or run Qwen or any other model yet.
Qwen and the combined arm are phase 2, to be started only when I ask.

## Ground rules
- `rulebook.py` is the source of truth for gold labels. Run `python test_rulebook.py` after any change to it or to `generate_cases.py`.
- Tune only on `data/cases_dev.jsonl`. Do not read `labels_test.jsonl` or run models on the test split until I say the setup is frozen.
- Never pass labels, `gold_*`, `scenario` or `is_hard` to a model. Join predictions to labels on `case_id` only when scoring.
- Save every raw model output and probability to disk (JSONL, one file per run, in `runs/`) so scoring can be rerun without rerunning models.
- Record for every run: model name, revision, dtype or quantization, hardware, date.

## Phase 1 goal: how well does Kev decide on its own?
- Input: the structured case fields as the state (everything in the cases files except the free-text statements, which can be added in a later experiment).
- Options: the four outcomes, APPROVE_BUYER, DENY, REQUEST_EVIDENCE, ESCALATE, as one `choice` question. Also test a variant with short plain-language descriptions of each outcome.
- Optional second question: a `choice` over rule IDs, to measure rule-citation accuracy. Treat as an experiment; only keep it if the server handles it well.
- Metrics: outcome accuracy and per-class precision/recall, wrong-refund vs wrong-deny counts, calibration (reliability table or ECE), escalation behavior (does low confidence line up with ESCALATE and REQUEST_EVIDENCE cases), consistency across repeated runs, latency and memory per case. Break results down by `gold_rule` and by `is_hard`.
- Also record the confidence threshold analysis: for thresholds from 0.5 to 0.95, what share of cases would be decided automatically and what the error rate is on those.

## Environment
- Hardware: personal Windows laptop, 16GB RAM shared between CPU and GPU. Not production, so slow runs are fine; batch them.
- Memory is the main risk. The Kev-4B model card describes it as sized for one 24 GB GPU or a 32 GB Apple Silicon Mac; a third-party Mac setup lists about 16 GB while loading it. Kev-0.8B needs roughly 4 GB. **Start with Kev-0.8B.** Try Kev-4B only after the harness works, close other heavy apps first, and stop and report if it fails to load or swaps heavily.
- The Kev README lists CUDA, ROCm and Apple Silicon (MLX) backends and does not mention Windows or integrated GPUs. Check whether it runs natively on Windows and on CPU. If not, try WSL, capping its memory in the WSL config file and checking whether the GPU is visible from inside it.
- Check what is already installed (Python, uv, git, GPU, WSL) before installing anything. Ask before downloading large files.

## Kev setup (official route, not Ollama)
Kev is a LoRA adapter plus a pointer-style decision head on a Qwen base model. It scores supplied options and does not generate text, so generic chat runtimes such as Ollama or LM Studio cannot run the decision head.

Sources (verify the commands against the current README before running):
- Code: https://github.com/jaredpalmer/kev
- Weights: the Kev collection on Hugging Face, https://huggingface.co/collections/jaredpalmer/kev-6aad9d0ea49f2589665e07cd
- Release with checkpoints and SHA-256 checksums: https://github.com/jaredpalmer/kev/releases/tag/kev-1.0
- Browser demo (nothing to install): https://huggingface.co/spaces/jaredpalmer/kev, useful for a quick sanity check of a few dev cases.

Steps:
1. Install uv (https://docs.astral.sh/uv/) and git.
2. `git clone https://github.com/jaredpalmer/kev.git && cd kev && uv sync --extra serve`
3. Start the server with a pinned revision, for example
   `KEV_DTYPE=bf16 uv run --extra serve python -m kev.serve --run jaredpalmer/kev-4b@v1.0 --port 8009`
   (PowerShell: set `$env:KEV_DTYPE="bf16"` first). For 0.8B, find the exact repo ID in the collection (likely `jaredpalmer/kev-0.8b`) and use its `v1.0` tag.
4. The server copies TypeSafe's System One API: `POST /v1/systemone`, with a `state` and typed `questions` (`choice`, `yes_no`, `score`). Read the README for the exact request format and write a small client; do not guess field names.
5. Verify the server with one or two dev cases before running the full dev split.

Precision: use the same dtype for every run and record it. Avoid community GGUF or 4-bit conversions for the main benchmark, because calibration is a core metric; one community 4-bit conversion reported probability drift and changed choices. If a quantized Kev is ever used, label the run clearly and compare it against full precision on a dev subset.

## Phase 2 (later, only when asked)
Qwen alone (text input), then Kev + Qwen combined (Qwen extracts facts, Kev decides; also a routing variant where Kev decides first and escalates when confidence is low). Use an instruct Qwen of about 4B, note that Kev sits on a base model, and use temperature 0 with a fixed JSON schema.

## Working style
- First step: report what is installed and whether Kev-0.8B and Kev-4B can run on this machine, before writing the harness.
- Build the harness to run one model at a time on the dev split and write raw outputs to `runs/`.
- Keep changes small and tell me what each run produced; do not tune against the test split.

## Phase 1 scripts (already written and tested against a mock server)
- `run_kev.py` sends dev cases to the local Kev server and saves raw probabilities to `runs/`. Flags: `--rules` (rule summary in the instructions), `--derive` (precomputed day counts, since Kev is weak at date arithmetic), `--with-text` (statements and evidence lists), `--limit N`, `--tag kev-4b-v1.0`. Resumable. Refuses the test split without `--allow-test`.
- `score_run.py --run runs/<name>.jsonl` prints accuracy, per-class results, confusion matrix, costly errors, calibration (ECE, Brier), the auto-decide threshold table and breakdowns by rule and difficulty.
- Planned experiment order on Kev-0.8B (dev only): `plain`, then `--rules`, then `--rules --derive`, then `--with-text`. Compare runs in a short table. Server: port 8009, `jaredpalmer/kev-0.8b@v1.0`, CPU, float32.
- Do not change the scripts to peek at the test split.
