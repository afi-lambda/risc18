# Brief (round 2): the 5.6 ns EBR read cost — model or silicon, and what follows

Follow-up to `advice_restart_arch.md`. You reviewed the restart architecture for an
18-bit ECP5 core and ranked the levers; this round is narrower: the single number the
whole two-cycle decision rests on. Answer in prose, numbered to the questions. No RTL.

## What changed since your last review

1. **The two-cycle probe was built and measured** (`src/risc18/probe_mem_pipe.py`):
   EBR read -> fabric register (cycle N), register + operand -> add -> EBR write
   (cycle N+1). Seeds 1/2/3/7 at a 150 MHz constraint: **150.08 / 149.93 / 145.18 /
   145.10 MHz**. Every seed has exactly 5.6 ns of "logic" — which is the DP16KD
   clock-to-out, not logic — plus 1.1-1.3 ns of routing. The reported path ends at the
   split register, so the write half is shorter in every seed. So: 120 MHz is met with
   21 % margin, 150 MHz is missed by 3.3 % at the worst seed.

2. **The single-cycle form measures 9.0 ns** (`probe_ebr_alu_ebr.ProbeW`): EBR DOA ->
   18-bit add carry chain -> second EBR's write address/data. Path quoted from the
   routed log: `src.0.0.DOA2` 5.6 ns -> 1.9 ns net -> `dst_w_data_CCU2C_*` carry chain.
   For reference the same chain with a 16:1 operand mux is 10.4 ns and with an 18x18
   multiply 13.2 ns.

3. **The design does not use the EBR's own output register.** Yosys hardcodes
   `REGMODE_A("NOREG")`/`REGMODE_B("NOREG")` in `ecp5/brams_map.v` (lines 120-121 and
   360-361), so every inferred memory has the block's output register bypassed and the
   read data goes to a *fabric* flop. Two attempts to price `REGMODE=OUTREG` failed:
   - hand-instantiating `DP16KD` with `REGMODE_A="OUTREG"` (and `"NOREG"` as control):
     nextpnr prints `No interior timing paths found in design` for both — no timing
     arcs for a raw cell instance, so it refuses to time it;
   - patching `REGMODE_A` to `"OUTREG"` in a post-synthesis netlist and re-running
     nextpnr: 346-442 MHz, with the EBR absent from the timing log entirely
     (`grep -c mem.0.0 pnr.log` = 0). That is the model dropping the cell, not a result.

So the 5.6 ns is nextpnr's number, and I have no silicon measurement of it. Device:
ECP5 `LFE5U-25F-6CABGA256`, speed grade -6, 25 MHz board oscillator, PLL available,
UART telemetry harness on the board already (`tools/hil_status.py`, proven at 60 MHz).

## Questions

1. **Is 5.6 ns a plausible DP16KD clock-to-out for LFE5U-25 at speed grade -6**, in
   your understanding of the part? If the vendor's clock-to-DO for a 1024x18 NOREG read
   is materially lower, say roughly what it should be and what the discrepancy implies
   about nextpnr's model. If 5.6 ns is about right, say that too — I need to know
   whether to distrust the model before I build an architecture on its floor.

2. **How would you settle model vs silicon on this board with the least effort?** The
   harness can program SRAM and read a UART line at 60 MHz; the design is Amaranth and
   the flow is yosys/nextpnr. I am considering a ring oscillator gated by a measured
   EBR read path, or a self-timed counter that starts on the read launch and stops on a
   delayed replica. Name the experiment you would actually run, its expected resolution,
   and the failure mode that would make it measure the wrong thing.

3. **If the real number is 3 ns rather than 5.6 ns, does the two-cycle decision
   change?** Specifically: at 3 ns the single-cycle EBR->add->EBR chain would be about
   6.4 ns (3 + 2.0 + write setup + routing), which fits 120 MHz. Would you then prefer
   the single-cycle form and a shallower pipeline, or still the latency-2 contract with
   more timing margin? Give the criterion you would use, not a preference.

4. **Rank the ways to reduce the memory read cost that do not need a toolchain change**:
   distributed RAM instead of EBR for the register file and small tables, 36-bit EBR
   mode, two blocks interleaved by address bit, a narrower memory with byte/word select,
   or something I have not listed. For each, say what it costs in LUTs, latency or
   ports, and whether it moves the read stage or only the width.

5. **What is the failure mode you have not seen me list** in this specific plan — the
   one that makes a latency-2 memory core on this part end up neither fast nor
   efficient, once the EBR floor is what it is?

Constraints: prose, no code. If you think a number here is wrong, say which and why
first.
