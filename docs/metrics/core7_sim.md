# Core7 — simulation CPI and routed numbers

**Gate verdict, controller's read of this file: SIM green, PnR RED.** The suite is
355 passed including 38 new tests, and the retire trace matches `golden.Machine.trace`
and `Core` on every program. But `Core7Top` routes at 64.49-70.56 MHz against the
120 MHz gate, and `Core6Top` on the same flow is 77.92 MHz, so the seven-stage restart
is *slower* than the core it was meant to replace. Merged as an instrument, not as a
signoff: see section 5 and `docs/plans/2026-11-19-memory-centric-18bit.md`.

`src/risc18/core7.py`, the seven-stage restart core of `docs/ISA.md` section 7
(F · IQ · D · R · X · M · W). Everything below was measured on this worktree;
nothing here is estimated. Commands are given with each table so a number can be
re-taken.

## 1. Retire trace and fill

`tests/test_core7.py` holds the core against `golden.Machine` on every program in
`tests/`, and against `Core` and `Core6` on the same words: the `we=1, rd!=0`
rows are identical triple-for-triple, and the retire pulse count equals
`golden.retired`. Word 0 retires on cycle 6 of the test driver's count, the same
fill `Core6` has: F and IQ are the presenting and capturing edge of one EBR
access, exactly as Core6's `F1` is, so the register chain is the same five
boundaries (`fq -> fd -> rx -> xm -> mw`). What the R register moves out of the
X cycle is the register-file read and its bypass select, not a cycle of latency.

## 2. CPI on the measurement programs

    cd /home/alain/risc18-wt/stage5
    TMPDIR=... PYTHONPATH=src .venv/bin/python -m pytest tests/test_core7.py -q

Same programs, same data images, same driver (`_drive` from
`tests/test_core.py`) for all three cores.

| program | retires | Core7 cycles | Core7 CPI | Core6 cycles | Core6 CPI | Core cycles | Core CPI |
|---|---:|---:|---:|---:|---:|---:|---:|
| `CORE_TOP_PROGRAM` (16-word) | 13 | **23** | **1.769** | 24 | 1.846 | 21 | 1.615 |
| `store_load_loop_program` | 35 | **60** | **1.714** | 60 | 1.714 | 56 | 1.600 |
| `counted_loop_program` | 19 | **39** | **2.053** | 39 | 2.053 | 35 | 1.842 |
| `sum_loop_program` (10-word sum) | 37 | **82** | **2.216** | 82 | 2.216 | 73 | 1.973 |
| `load_store_program` | 6 | 7 | 1.167 | 7 | 1.167 | 7 | 1.167 |
| `jal_program` | 5 | 15 | 3.000 | 15 | 3.000 | 13 | 2.600 |
| `jalr_program` | 7 | 17 | 2.429 | 17 | 2.429 | 15 | 2.143 |

Core7 finishes the 16-word program one cycle *earlier* than Core6 (23 vs 24) and
is level on every other program: nothing is lost to the deeper schedule, and the
one dependency Core6 pays a bubble for -- a MUL at distance 1 -- is free here
(section 3). `sum_loop` and `counted_loop` are dominated by the taken-branch
penalty, which is the same 5 slots as Core6 (section 4).

## 3. Bubbles per distance (the table of section 7.2)

Measured as `run.cycles - len(program)` on straight-line programs with one
dependency each; each number is asserted in `tests/test_core7.py`.

| dependency | distance 1 | distance 2 | distance 3 |
|---|---:|---:|---:|
| ALU -> consumer | **0** | **0** | **0** |
| MUL -> consumer | **0** | **0** | — |
| load -> consumer | **1** | **0** | **0** |

* ALU at distance 1: the producer is in X while the consumer reads its operands,
  so its result is taken combinationally into `rx` (X -> R bypass).
* MUL at distance 1: the product is registered at the end of the producer's X and
  is muxed onto the ALU input while the producer sits in M — the **M -> X input
  bypass** of section 7.2, registered one cycle early (see section 5).
* load at distance 1: the word is registered only at the end of the load's M, one
  cycle after the consumer's X would open, so the consumer is held in D for
  exactly one cycle; from distance 2 the word is in W/M and costs nothing.

A 4-deep MUL chain and a 4-pair LD/use program are also asserted: 0 bubbles and
4 bubbles (CPI 1.333) respectively.

## 4. Taken transfer

| transfer | retire slots |
|---|---:|
| taken branch | **5** |
| taken JAL / JALR | **5** |
| not-taken branch | **0** |

5 is the same as `Core6` pays and it is what the depth implies: the decision is
registered in the X cycle and applied on the next edge, so the two-cycle squash
window (`ct_valid` + the edge behind it) covers the R slot, the D slot, the `fq`
slot and the two words the EBR still holds from the old PC. Note for the
contract owner: section 7.3 says "recovery is 2 cycles" while also freezing the
decision at the X/M edge; with a decision at the X/M edge the retire-stream cost
of a taken transfer in a seven-stage machine is 5 slots, and 2 is what the
window itself spans (and what a *D-stage* speculative redirect would cost in
retire slots — the arrangement section 6 documents as the 48 MHz one). Core7
implements the X/M decision, so its window is 2 cycles and its retire cost is 5
slots.

## 5. Routed build

    tools/synth.py --top risc18.tops:Core7Top --name core7_seed1 --freq 120 --seed 1

| build | LUT4 | FF | DP16KD | DSP | fmax seeds 1 / 2 / 3 | worst |
|---|---:|---:|---:|---:|---|---:|
| `Core7Top` | 1471 | 697 | 2 | 1 | 68.20 / 70.56 / 64.49 MHz | **64.49 MHz** |
| `Core6Top` (same flow, same seed 1) | 1574 | 693 | 2 | 1 | 77.92 MHz | 77.92 MHz |
| `Core7Top`, one-bubble fallback | 1580 | 695 | 2 | 1 | 75.58 MHz | 75.58 MHz |

**120 MHz is missed, and it is missed by `Core6Top` on this flow as well**
(77.92 MHz at the same constraint and seed). The critical path in every Core7
seed is the **X stage**: a flop (`xm_mul`, i.e. the registered M -> X bypass
select, in all three seeds) -> the input mux -> the ALU carry chain -> the
`x_value` select -> the data-EBR address and write-enable. Logic 2.9-4.4 ns,
routing 10.3-12.7 ns. The path is routing-dominated at ~1.5 kLUT, so logic depth
is not the whole story; the same shape (operands -> ALU -> dmem address) is
Core6's critical path too, 3.4 ns shorter at seed 1.

The section 7.2 fallback was built and measured, not assumed:

| variant | distance-1 ALU/MUL | CPI 16-word | CPI store-load | CPI counted-loop | fmax seed 1 |
|---|---|---:|---:|---:|---:|
| M -> X bypass (default) | 0 bubbles | 1.769 | 1.714 | 2.053 | 68.20 MHz |
| one-bubble fallback | 1 bubble | 2.077 | 1.857 | 2.368 | 75.58 MHz |

MIPS at those numbers (`fmax / CPI`): the default gives 38.5 / 39.8 / 33.2, the
fallback 36.4 / 40.7 / 31.9 — within 10 % either way, and *both* are below the
5-stage core's 45.5 MIPS at 73.48 MHz / CPI 1.615 (and below Core6's 42.2 / 45.5
/ 38.0 on the same flow), because no build of the seven-stage core reaches
120 MHz here. The default keeps section 7.2's normative 0-bubble row (the
contract's own preference) and the better CPI on two of the three programs; the
fallback is one constructor argument away (`Core7(..., x_bypass=False)`) and is
exercised in `tests/test_core7.py`.

Not measured here: HIL, the board MIPS loop, and seed 7 for Core7 (the brief's
PnR gate wants 1/2/3/7; 1/2/3 are above). `docs/metrics/NOTES.md` is unchanged.
