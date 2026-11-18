# Milestones and gates

One PR per milestone, base = latest `origin/main`. Every milestone must leave
the repository green.

## Global gate (controller-verified, never worker-reported)

```
.venv/bin/python -m pytest tests/ -q           # 100% pass, whole suite
.venv/bin/python tools/synth.py --top <top> --name <mN_name>
```

The synth run must produce `build/metrics/<mN_name>.json` with non-zero
utilization, and the milestone's `.md` summary is committed under
`docs/metrics/`. Post-place-and-route numbers only (nextpnr), never yosys
estimates alone.

Rules:

* Frozen files: `src/risc18/isa.py`, `src/risc18/golden.py`, `tools/asm.py`,
  `docs/ISA.md`. Changes need controller approval in the PR body.
* Every milestone adds tests; no milestone deletes or weakens existing tests.
* No new dependency without controller approval.
* New RTL is Amaranth, exercised in `amaranth.sim.Simulator`; tests never
  merely mirror Python helpers.
* Synth tops wrap unit I/O in registers so the measured path is meaningful,
  and expose `.ports`. Inputs and outputs both registered: nextpnr only reports
  fmax when an interior register-to-register path exists, and M4+ milestones
  must show a non-empty fmax.

## Milestones

| M | Branch | Scope | Acceptance |
|---|---|---|---|
| M0 | `feat/m0-decoder` | `risc18.decoder.Decoder` + `isa` conformance tests | Every format decodes exactly like `isa.decode()`; fuzz over seeded random words; all fields of section 4 checked; metrics recorded |
| M1 | `feat/m1-regfile` | `risc18.regfile.RegFile` | 16×18, 2 comb reads, sync write, r0 hardwired, write-through read verified, reset clears; metrics |
| M2 | `feat/m2-alu` | `risc18.alu.ALU` + `risc18.branch.BranchUnit` | 8 ALU ops incl. all shifts, signed compares; boundary + random vectors vs golden; metrics |
| M3 | `feat/m3-pc-branch` | `risc18.pc.PCUnit` (PC register + next-PC mux + redirect) | sequential, branch taken/not, JAL/JALR targets, 16-bit wrap; metrics |
| M4 | `feat/m4-imem` | `risc18.imem.InstrMemory` + fetch stage | 1-cycle registered read, enable freeze, preloaded program fetched in order, redirect next cycle; metrics |
| M5 | `feat/m5-core` | 4-stage F/D/X/W `risc18.core.Core` without data memory (LD/ST retire as no-ops: no writeback, no memory) | Arithmetic/branch/jump/LUI/shift/halt programs match golden lockstep; no forwarding stalls correct; cycles = N + one bubble per distance-1 dependency (exact, asserted); metrics |
| M6 | `feat/m6-dmem` | `risc18.dmem.DataMemory` + LD/ST in the core (M stage) | Load/store programs match golden incl. memory contents; M-stage bypass so only distance-1 stalls; load-use exactly one bubble; out-of-range read 0/write ignored; metrics |
| M7 | `feat/m7-forwarding` | X-stage forwarding (M and W forwarding already in the M6 baseline) + load-use-only stall | Distance-1 ALU chains retire at exactly N cycles for N instructions; branch/store/JALR operands forwarded; load-use exactly one bubble; branch flush correctness; seeded random-program lockstep; metrics |
| M8 | `feat/m8-multiplier` | `risc18.mul.Multiplier` + DSP measurement | Products match golden for edge/random operands; one `MULT18X18D` and no LUT multiplier in the DSP tops (the core claim rests on `docs/metrics/m8_core.md`); registered-DSP variant measured, golden-clean and rejected (+1 bubble per MUL, no resolvable gain); core keeps the inline multiply after measuring a +159 LUT4 remap cost for the submodule form |
| M9 | `feat/m9-timing` | Timing closure + resource report | Control transfer resolved in X with a registered redirect; CoreTop fmax ≥ 60 MHz at the 60 MHz constraint across a seed sweep; full top with a 25->60 MHz PLL (M9b); LUT/FF/EBR/DSP table in `docs/metrics/m9_core.md` and `docs/RESULTS.md` (M9b); critical path documented; all tests green |

## Worker protocol

1. `git fetch origin && git switch -c <branch> origin/main` in
   `/home/alain/risc18`.
2. Implement only the milestone scope. Do not push, do not open PRs, do not
   touch `main`.
3. Run the full suite and the synth gate. Commit (`feat: ...`), including the
   metrics `.md` in `docs/metrics/` for your milestone.
4. Report: files, test counts, synth numbers, known gaps.

The controller then re-runs both gates, inspects the diff, pushes, opens the
PR, runs two independent reviews, and merges only when clean.
