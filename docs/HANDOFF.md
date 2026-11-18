# risc18 — handoff

State of the design after M9, for whoever (or whatever) picks it up next.
Everything below is measured on this machine unless it says otherwise.

## What the design is

An 18-bit Harvard RISC for the Colorlight 5A-75B (`LFE5U-25F-6CABGA256`), written
in Amaranth, built by `yosys` -> `nextpnr-ecp5` -> `ecppack`.

| item | value |
|---|---|
| data / instruction width | 18 bits |
| registers | 16 x 18, r0 hardwired to zero, flops not EBR |
| address width | 16 bits (program and data memories are 1024 x 18 each, one DP16KD apiece) |
| multiply | one `MULT18X18D`, inline in the core |
| pipeline | F/D/X/M/W with X-stage control transfer |
| CoreTop (core + output shell) | 1813 LUT4, 605 FF, 2 DP16KD, 1 DSP |
| FullTop (core + 25->60 MHz PLL + boot sequencer) | 1780 LUT4, 670 FF, 2 DP16KD, 1 DSP, 1 PLL |

## Pipeline, as built

```
F: PC register -> instruction EBR (clock-enabled read) -> FD register
D: decode, combinational regfile read, forward/hazard select
X: ALU/MUL/link/LUI, LD/ST effective address, branch compare + target add
M: data EBR (store commits on the X->M edge), load word
W: register writeback
```

Rules that are easy to break and expensive to debug:

* **Forwarding is X > M > W.** X forwards its result when it is not a load;
  M forwards its result or the load word; W is covered by the regfile's
  same-cycle write-through. Only a **load-use** dependency stalls, exactly one
  bubble; distance-1 ALU chains cost nothing.
* **Control transfer resolves in X and the redirect is registered**, so a taken
  branch or jump kills **four** fetch slots (the two decode/fetch slots plus the
  two words the EBR and FD registers still hold). That is deliberate: it removed
  the compare -> redirect -> PC mux -> FD capture loop that capped the design at
  ~48 MHz. Not-taken branches cost nothing.
* **HALT** freezes fetch in D, retires through W, and sets a sticky `o_halt`.
  A HALT in D on the same edge a control transfer resolves in X must not freeze
  the core or swallow the redirect (`~take_x` in `d_halt` is load-bearing).
* **Store commit is gated by reset** (`dmem.i_we & ~i_rst`); memory contents
  otherwise survive reset. Data EBR reads are `transparent=False`
  (read-before-write), matching the DP16KD primitive.
* **`o_target` / `ct_target`**: `BranchTarget` builds both `pc+1+imm` and
  `rs1+imm` in parallel (two independent adders, muxed on the low 16 bits) --
  nothing is shared. The registered `ct_target` is where a redirect is applied
  from.

## Interfaces that are frozen

`src/risc18/isa.py`, `src/risc18/golden.py`, `tools/asm.py` and `docs/ISA.md`
are the normative contract. `docs/ISA.md` also freezes the unit interfaces
(`RegFile`, `ALU`, `BranchUnit`, `InstrMemory`, `Fetch`, `DataMemory`,
`BranchTarget`, `PCUnit`, `Core`), the reset rules and the hazard policy.
`risc18.mul.Multiplier` is a documented option (the DSP measurement tops and the
registered variant), not part of the frozen contract. Changing any frozen item
requires an explicit controller approval recorded in the PR body.

## Gates

```bash
source .venv/bin/activate                                  # Python 3.11.15, Amaranth 0.5.10
.venv/bin/python -m pytest tests/ -p no:cacheprovider -q -o addopts=""   # 317 tests
.venv/bin/python tools/synth.py --top risc18.tops:CoreTop --name m9_core --freq 60 --seed 1
.venv/bin/python tools/synth.py --top risc18.tops:FullTop --name m9_full --freq 25 --seed 1
```

Only post-place-and-route `nextpnr` numbers count; `docs/metrics/*.md` are those
runs. **Always quote a seed sweep, never one run** — small netlist or naming
changes move individual seeds by up to 10 MHz on this device.

## Measured results (seeds 1/2/3/7)

| top | fmax (MHz) | worst |
|---|---|---|
| CoreTop | 65.82 / 67.91 / 70.01 / 73.89 | 65.82 (5.82 MHz over 60) |
| FullTop (PLL) | 73.67 / 71.80 / 72.26 / 69.42 | 69.42 (9.42 MHz over 60) |

CPI (`amaranth.sim`, not silicon): 1.615 on the 16-word measurement program,
1.600 on a five-iteration store/load loop, 1.842 on a five-iteration counted
loop. The bubbles are the
four killed slots per taken transfer plus one per load-use.

The PLL is the vendor `ecppll -i 25 -o 60` set: `CLKI_DIV=5, CLKFB_DIV=12,
CLKOP_DIV=10, CLKOP_CPHASE=4`, CLKOP fed back -> 60.000 MHz exactly, VCO
600 MHz. `nextpnr` derives that same 60.0 MHz for the CLKOP net itself, so the
FullTop metric's PASS/FAIL column is meaningful; its `--freq 25` value is the
25 MHz pad, not the core clock. (Before stage 4 this file quoted
`CLKI_DIV=1/CLKFB_DIV=24/CLKOP_DIV=10`: with CLKOP feedback that is 600 MHz of
CLKOP and a 6 GHz VCO, nextpnr rejected the VCO, and the board produced no
clock. `docs/RESULTS.md` has the equations and the witnesses.)

## What has *not* been done

- **Silicon timing is not measured.** The HIL harness (`HilTop`: the same core,
  the same `Pll`, plus a UART) has been loaded into SRAM and reports the
  simulated retire/cycle counts (`docs/metrics/hil5.md`), so the clock and the
  core are known to run — but every fmax above is static timing, and there is no
  PLL lock-time/jitter/duty measurement.
* No interrupts, no exceptions/traps, no byte accesses.
* Memories are 1024 words each. The 16-bit address space leaves headroom, but
  the frozen 10-bit rule (data `0x0000..0x03FF`, `PC >= 0x400` reads 0, out-of-
  range loads 0 and stores ignored -- ISA.md sections 1 and 5) has to change
  first, with controller approval. Nothing beyond one EBR per memory has been
  built or measured.
* The watchdog in `FullTop` restarts a hung program without clearing the data
  EBR, so a store-writing program restarts with dirty memory.

## Obvious next steps

1. **HIL bring-up**: load `FullTop`, observe `o_locked`/`o_halt`/`o_pc`, and
   check the core really retires the measurement program at 60 MHz.
2. **Recover the branch CPI**: the four-slot squash buys the clock but costs
   ~0.3 CPI on branch-heavy code. A distance-1 bypass out of `x_value` (not a
   plain operand-register split, which would break the zero-bubble contract) or
   a 1-bit branch prediction ("backward taken") are the natural levers.
3. **Area/timing polish**: the limiter is now the D->X operand load through the
   forward/hazard select, plus the fetch-hold cone on one FullTop seed. The
   branch-target twin adder sits on two of CoreTop's four critical paths but is
   never their tail, so collapsing it (~1% LUT4) would not move fmax.
4. **`tools/synth.py`**: record `--seed` in both the `.json` and the `.md`
   metrics (today it reaches only nextpnr and the log filename). The CLKOP
   constraint needs no `FREQUENCY NET` line any more: with the corrected
   dividers nextpnr derives 60.0 MHz for `boot_clk` and 120.0 MHz for the
   six-stage top.
