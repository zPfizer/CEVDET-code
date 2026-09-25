# Execution-state product adapter

The installed canonical map owns the selected 14 cases and 36 repetitions.
`harness_metrics.collapse(record, runs)` remains the case-level verdict owner.
`--smoke` defaults to one real EVAL-029-shaped source conversation and corrupts the
observed STATE and FS artifacts to test grader sensitivity. It is harness
evidence, not a canonical three-run acceptance result.
Use `--smoke --case EVAL-075` (and 076/077) for the distinct worker surfaces.
`--run` requires `--harness-evidence` paths to successful smoke traces whose
actual implementation, catalog and effective configuration match each run.
Keep smoke and canonical outputs in the same new candidate evidence directory
so the observed closure reference remains stable. Changed code requires a new
directory and a fresh smoke. Independent parallel runs must use processes;
the synthetic/native CODEX_HOME context is process-local, not thread-safe.
Run EVAL-077 smoke and canonical repetitions in a separate process from the
other cases. Its observed child-loader closure is distinct; keep that precise
closure rather than broadening the allowed loaded-file set.

`fixtures.py` contains synthetic user/assistant source conversations only.
The model receives those sources and the runtime-generated current state.
Expected predicates stay in `adapter.py` and never enter a model prompt.
Native-call evidence records only synthetic final schema fields and source,
prompt and output digests. Invalid JSON is never stored as raw text; reasoning
fields and credentials are not collected.

The semantic execution surface is production `execution_state.propose` using
real `flush.run_codex`, then `companion_memory.publish`, then
`hook.build_session_context`. The memory publication envelope contains source
text, not a hidden target state. This does not claim full flush/compiler E2E
coverage. Worker cases 075–077 require the actual worker and hook dispatcher.

Run `python -B -m unittest discover -s evals/execution-state -p test_adapter.py`.
Unit tests are adapter checks and must not be labelled product EVAL passes.

The CLI requires explicit `--map`, `--harness-tools`, and a new `--output` path.
Without `--run` or `--smoke`, it writes a NOT_RUN inventory. Existing artifacts
are never overwritten. A source/config/native identity gap yields INVALID.
Working fixtures are newly owned temporary directories outside the evidence
tree. Every post-state file is copied and checked before scoped reset; only
files created by that disposable run may be removed. The original seed files
and unrelated sentinel remain byte-identical, and cleanup records the exact
file-set digest. Historical candidate evidence is never a cleanup target.
Each fixture explicitly names its mutable runtime paths. Changed immutable
sources or new files outside that list refuse cleanup before mutation; target
bytes are checked again immediately before each removal or restoration.
Case 031 includes a real deterministic source verification command and binds
its receipt to the agent-visible transcript and preserved execution event.
No auth file is copied or read. Synthetic identity discovery uses a fixture
CODEX_HOME; the real native process keeps the existing authorized native home.

EVAL-098 is deferred to 6.2; EVAL-100 is deferred to 6.3. Neither is counted PASS.
