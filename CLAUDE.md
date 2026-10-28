# Resolution Center

Learning project (not production): AI-assisted dispute resolution for INR and SNAD cases, built to practice RAG, agents, workflows, MCP, prompt and model evaluation. All data is synthetic, policy is invented, and "execution" is a mock that moves no money. See README.md for the full overview and architecture.

Design principle: the AI recommends and explains, deterministic rules enforce policy, authorized humans execute consequential actions.

## Current state
- Built and tested: rulebook, policy layer, approvals with a hash-chained audit log, evidence agent with mock APIs, BM25 multi-index RAG, MCP server, reviewer console and CLI, gold-set tooling, eval harness, Kev runner and scorer.
- In progress: Kev-0.8B experiments on the dev split. Done: `plain` (23.3% accuracy) and `--rules` (50.0%). Not yet run: `--rules --derive`, `--with-text`, Kev-4B.
- Not wired: a model's recommendation is not fed into `policy.decide` in the live path (the `ai` argument exists but is unused). Models are evaluated offline only.
- **Do not install, download or run Qwen or any other model** until I ask. Kev only for now.

## Ground rules
- `rulebook.py` is the source of truth for outcomes and gold labels. The final outcome in `policy.decide` always comes from the rulebook; an AI recommendation is recorded and compared, never obeyed.
- Changing or adding a rule means updating, together: `rulebook.py`, `test_rulebook.py`, the rulebook docx (`rag_indexes.py` parses it, and the filename must not change), the scenarios in `generate_cases.py`, and any affected policy and RAG tests. Then regenerate data and rerun all tests.
- The test split is sealed. Never read `data/labels_test.jsonl`, run models on `data/cases_test.jsonl`, or pass `--allow-test` until I say the setup is frozen. Tune on dev only. The service, console and eval harness must keep excluding the test split.
- Never pass `gold_*`, `scenario` or `is_hard` to a model. Join labels to predictions on `case_id` only when scoring.
- Gold labels must stay blind: do not edit `gold/label_sheet.csv` after `gold_tools.py compare`, and do not hand-edit `data/gold_labels.jsonl` (use `freeze`).
- Never hand-edit `audit/audit_log.jsonl`. Run `python review_cli.py verify` after any change touching approvals.
- The MCP server must never get tools that approve, override or execute. Only the console and review CLI do that.
- Thresholds live in one place each: `rulebook.py` (rule thresholds) and `policy.py` (AI confidence, auto-execute outcomes). Do not copy numbers into other files.
- `data/` is git-ignored except `data/gold_*.jsonl`. Regenerate the synthetic files with `python generate_cases.py --n 300 --seed 42 --out data` (a clone has no data until you do). Never commit synthetic data. Gold files are hand-labeled and not regenerable, so never overwrite or delete them. Changing the generator, rulebook or seed changes the data, so note it and rerun anything that depends on it.
- Save every raw model output and probability to `runs/` so scoring can be rerun. Record model, revision, dtype, hardware and date for every run.

## Commands
```
python generate_cases.py --n 300 --seed 42 --out data     # run first on a fresh clone
python test_rulebook.py        # also test_policy, test_approvals, test_evidence_agent, test_rag,
                               # test_eval_harness, test_gold, test_console, test_mcp_server
python console.py --port 8765                              # http://127.0.0.1:8765
python review_cli.py queue
python mcp_server.py                                       # stdio
python eval_harness.py --system rulebook                   # gold set; also --system knn
python eval_rag.py
python run_kev.py --limit 5            # smoke test; flags: --rules --derive --with-text --tag
python score_run.py --run runs/<name>.jsonl
```
Run all tests after any change and report the results.

## Code conventions
- Python 3.10+, standard library only, except the `mcp` package for `mcp_server.py`. Do not add dependencies without asking.
- Each test file runs as a plain script (`python test_x.py`) and also under pytest.
- Small, focused changes. Tell me what changed and what each run produced.

## Modules
`rulebook.py` rules and labels, `policy.py` decision and approval status, `approvals.py` queue and audit log, `evidence_agent.py` with `mock_apis.py` evidence gathering (pluggable planner, retries, call budget), `rc_service.py` shared service layer, `mcp_server.py`, `console.py` with `console/index.html`, `review_cli.py`, `retrieval.py` with `rag_indexes.py` (BM25; policy, guidance and precedent indexes), `generate_cases.py`, `make_gold_cases.py` with `gold_tools.py`, `eval_harness.py`, `eval_rag.py`, `run_kev.py`, `score_run.py`.

## Environment
- Personal Windows laptop (PowerShell), 16 GB RAM shared between CPU and GPU. Not production, so slow runs are fine; batch them.
- Memory is the main risk. Kev-0.8B needs roughly 4 GB and runs on CPU in float32. The Kev-4B model card describes larger machines (24 GB GPU or 32 GB Apple Silicon), so try 4B only after 0.8B experiments are done, close heavy apps first, and stop and report if it fails to load or swaps heavily.
- Ask before downloading large files.

## Kev setup
Kev is a LoRA adapter plus a pointer-style decision head on a Qwen base model. It scores supplied options and does not generate text, so Ollama and LM Studio cannot run it. Use its own server.

- Code: https://github.com/jaredpalmer/kev. Weights: the Kev collection on Hugging Face. Verify commands against the current README.
- The Kev repo is a separate clone, not part of this repo. It needs Python `>=3.12,<3.14`.
- Windows Application Control blocked the Python that uv downloads itself (error 4551). Workaround: install a signed Python 3.13 (for example `py install 3.13`), set `$env:UV_PYTHON_DOWNLOADS="never"`, and run `py -m uv sync --extra serve --python "<path to python 3.13>"`. Later runs use `uv run --no-python-downloads`.
- Start the server from the Kev clone: `py -m uv run --no-python-downloads --extra serve python -m kev.serve --run jaredpalmer/kev-0.8b@v1.0 --port 8009`. If the Hugging Face download fails with a CAS or Xet error, set `$env:HF_HUB_DISABLE_XET="1"` and retry.
- API: `POST /v1/systemone` with a `state` and typed `questions` (a `choice` question lists options under `criteria`). `run_kev.py` already does this; read the README before changing the request format.
- Use one dtype for all runs and record it. Avoid community GGUF or 4-bit conversions for the main benchmark, because calibration is a core metric.

## Experiment order (Kev, dev only)
`plain` and `--rules` are done. Next: `--rules --derive`, then `--with-text`, then Kev-4B if memory allows. Before running, state the stopping rule: if no variant gets close to the rule engine, conclude that this Kev size cannot follow the rules and move on. Report results as a short table, with confidence intervals where n is small.

## Next steps (in order; confirm with me before starting each)
1. Housekeeping: delete "- Copy" files and stray duplicates, decide which gold set is canonical (10 or 25 cases), label the rest.
2. Rulebook v0.2: fix gaps found in `gold/disagreements.csv`, regenerate data, rerun tests.
3. Finish the Kev experiments above.
4. Realistic variant: cases with only system facts and free text, where a model extracts the interpreted fields (`snad_issue`, `evidence_conflict`, `listing_matches_item`, `packaging_evidence`) and the rules decide. Score extraction per field and end to end.
5. Wire a model recommendation into `policy.decide` and measure how many errors the disagreement flag catches.
6. Only when asked: Qwen as extractor and an LLM planner for the evidence agent.
