# Plan — six-stage 18-bit core: feasibility, then build

Status: approved by the controller on 2026-11-18 for supervised execution with one
worker per stage, one PR per stage, exact-head review, controller merge.

## Question the plan answers

The 5-stage core closes at 73.48 MHz with a fetch-side critical path that contains
two memory reads (instruction EBR then distributed-RAM register file). Measured
stage-closure probes (stage 0) say a single EBR read with its consumer registered
runs at ~150 MHz and an EBR read plus decode still runs at 150 MHz, while the X
stage with the multiply in its select path runs at 101.6 MHz and with the multiply
result registered at 129.9 MHz. So: is a 6-stage cut, targeting 120 MHz, worth
building, and does it actually close after place and route?

## Stages

### Stage 0 — stage-closure probes (CONTROLLER, done)

Artefacts: `src/risc18/probe_ebr_read.py`, `probe_ebr_alu_ebr.py`,
`probe_six_stages.py`; metrics in `build/metrics/probe_*`; summary in
`docs/metrics/NOTES.md`; the design review in `docs/review/`.

Evidence: fetch-in 6.7 ns / 149.93 MHz; EBR read + decode 6.7 ns / 150.08 MHz;
EBR -> add -> EBR 9.0 ns / 111.53 MHz; EBR -> mul -> EBR 13.2 ns / 75.76 MHz;
X with mul in the select path 9.85 ns / 101.62 MHz (misses 120); X with the mul
result registered 7.7 ns / 129.85 MHz; X without mul 5.5 ns / 181.79 MHz.

Consequence carried into stage 1: the multiply result must be registered, so MUL
becomes latency-2 and a dependent MUL costs one bubble. That is a design decision,
not an implementation detail.

### Stage 1 — `Core6` in simulation (WORKER)

Deliverable: `src/risc18/core6.py` with `class Core6(program=(), data=None, ram_rf=False)`,
same external ports as `Core` (`o_pc`, `o_halt`, `o_retire_*`), plus
`tests/test_core6.py`.

Schedule:

```
F1  PC register -> imem EBR (clock-enabled read) -> fetch register
F2  decode (combinational) + register-file read-address select
D   register-file data + forward select + load-use detect
X   ALU / registered multiply / branch compare + target add; dmem addressed here
M   dmem word returns
W   register-file writeback
```

Hard rules:

- Reuse the frozen units (`isa`, `decoder`, `alu`, `regfile`, `ramrf`, `imem`,
  `dmem`, `branch`, `pc`). Do **not** modify `isa.py`, `golden.py`, `asm.py`,
  `docs/ISA.md`, or `core.py`.
- Control transfer resolves in X; the redirect is registered (unchanged policy).
- Multiply result registered in X before the result-select tree.
- Store commits only from X, gated by reset, exactly as the 5-stage core does.
- The 5-stage `Core` and the existing suite must remain untouched and green. The
  count at this head is **234 passed** (`pytest tests/ -q -o addopts=""` on
  `5673459`); the older 222 figure in HANDOFF is stale, so a worker that reports 222
  is quoting a document instead of running the suite.

Gates (all must pass, exact commands):

```
.venv/bin/python -m pytest tests/ -p no:cacheprovider -o addopts="" -q
.venv/bin/python -m pytest tests/test_core6.py -p no:cacheprovider -o addopts="" -q
```

New tests must pin: (a) lockstep against `golden` on every existing program the
5-stage tests use, (b) the taken-branch penalty in cycles, measured, asserted as an
exact number, (c) the load-use bubble, (d) the dependent-MUL bubble, (e) CPI on the
same three programs the 5-stage CPI table uses, asserted `<= 2.64` (break-even at
120 MHz against 45.50 MIPS).

Kill criteria: if lockstep cannot be achieved without changing the ISA or the
golden model, stop and report. If measured CPI > 2.64, report RED with the numbers
and do not weaken the bound.

### Stage 2 — routed build at 120 MHz (WORKER, conditional on stage 1 green)

Deliverable: `Core6Top` and `Full6Top` in `tops.py` (additive), `--top` reachable
from `tools/synth.py`, PLL at 120.000 MHz (reject any divider set that is not exact),
and a metrics table in `docs/metrics/` for seeds 1/2/3/7 plus both tops.

Gate: worst seed `>= 120.0 MHz` on `Full6Top`; report the per-seed critical path
classes and whether reset distribution is the limiter. Kill: worst seed `< 110 MHz`
-> report, do not tune around it.

### Stage 3 — hardware bring-up (WORKER, conditional, may block)

3a: the existing 5-stage `FullTop` at 60 MHz on the board, with a UART status
harness reporting retired count, taken-redirect count, lost-issue cycles and
dependency stalls for a fixed program, plus an edge-level trace of one taken loop
branch. Gate: on-board CPI within 5 % of simulation for that program.
3b: only if stage 2 passed, the 6-stage at 120 MHz, same program, same counters.

Transport probe first (30 s): confirm the board answers on `/dev/ttyUSB1` and that a
known bitstream programs. If it does not answer, mark BLOCKED by transport and stop
rather than running the ladder.

## Run protocol

- One worker per stage, sequential. Each worker gets its own `git worktree` at the
  controller's HEAD, a branch named `feat/<stage>`, commits, pushes, and opens a PR.
  Workers never merge.
- Controller reviews at the exact PR head (file list, diff, gates rerun in a clean
  worktree), requests bounded rework on findings, merges only that head, then
  verifies the merged tree, deletes the branch and prunes.
- Worker closeout is machine-readable: phase, branch, HEAD SHA, changed files, exact
  commands with totals, PR URL, blocker.
- Measured numbers are not copied from this plan: a worker that measures a different
  number reports the difference and the file it read.
