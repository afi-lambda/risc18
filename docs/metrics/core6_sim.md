# Core6 (six-stage) — simulation metrics and contract

Stage 1 of `docs/plans/2026-11-18-six-stage-pipeline.md`. `src/risc18/core6.py`
implements `Core6` (same ports as `Core`), simulation only: no synthesis, no
routing, no PLL. Everything below is `amaranth.sim` on this head, measured by
`tests/test_core6.py` (41 tests; suite total 275).

## Schedule

```
F1  PC register -> imem EBR (clock-enabled read) -> fetch register ``fq``
F2  decode (combinational, from ``fq``) + register-file read-address select,
    registered into FD (registered reads of the file for the RAM build)
D   register-file data + forward select + load-use / mul-use detect
X   ALU / registered multiply / branch compare + target add; dmem addressed
M   dmem word returns (and the registered product)
W   register-file writeback
```

The one structural change against `Core` is the register between the EBR output
and the decode. Everything downstream of FD (forwarding priority X > M > W, the
W/W-1 bypasses of the RAM file, the memory schedule, the registered redirect,
the HALT freeze, the retire trace) is the five-stage policy unchanged.

Two consequences, both pinned by exact cycle counts:

* **Taken transfer costs 5 killed fetch slots**, not 4: the fall-through the FD
  register captured on the resolving edge, the slot in D one edge later, the
  slot sitting in `fq`, and the two words the EBR latches from the old PC while
  the redirect moves it. Measured: one taken BEQ = `retires + 5`; two = `+10`.
* **MUL is latency 2.** The 18x18 product is registered in X (`x_prod`) and
  enters a select tree only in M, so a distance-1 consumer costs one bubble and
  distance 2 costs none. Load-use is still exactly one bubble.

`fq` must be *held* on a stall: the fetch's EBR read path is disabled there, so
its output still carries the word *after* the one in `fq` and re-capturing would
overwrite the instruction the stall is holding D for. A stale word is stopped
from being decoded by `fq_valid`, not by the squash window alone — the window
closes one cycle before the target word reaches the register.

## CPI (cycles from first retire to last, divided by retired)

| program | retired | cycles | Core6 CPI | `Core` CPI |
|---|---:|---:|---:|---:|
| `CORE_TOP_PROGRAM` (16 words, 3 squashed) | 13 | 24 | **1.846** | 1.615 |
| 5-iteration store/load loop | 35 | 60 | **1.714** | 1.600 |
| 5-iteration counted loop | 19 | 39 | **2.053** | 1.842 |

Break-even at 120 MHz against the 5-stage core's 45.50 MIPS (73.48 MHz / 1.615)
is CPI 2.64, so all three clear the budget, the worst with 22 % of margin. The
extra cycles are fully accounted for: +2 slots per taken transfer (2 transfers
in the 16-word program, 4 in each loop), plus 8 → 9 for the one distance-1 MUL
consumer in `CORE_TOP_PROGRAM` (word 6 shifts the product of word 5).

## Gates

```
.venv/bin/python -m pytest tests/ -p no:cacheprovider -o addopts="" -q
    275 passed
.venv/bin/python -m pytest tests/test_core6.py -p no:cacheprovider -o addopts="" -q
    41 passed
```

Both register-file builds (`RegFile` and `RegFileRam`) are exercised: the
lockstep tests are parametrised over `ram_rf` and the RAM build carries the same
W/W-1 bypass structure as `Core`.

Stage 2 (routed `Core6Top` / `Full6Top` at 120 MHz) is what decides whether the
cut pays; this note claims nothing about fmax.
