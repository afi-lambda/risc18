# hil5 — HIL status harness, first on-board measurement

Stage 3a bring-up: one UART line per configuration, carrying the counters the
five-stage core produced at 60 MHz. Everything below was produced by this repo
on the Colorlight 5A-75B over JTAG/SRAM (`openFPGALoader -b colorlight`, never
`-f`); the board's flash still holds another project's design.

## What was built

| item | value |
|---|---|
| top | `risc18.tops:HilTop` (FullTop + `risc18.hil.HilStatus`) |
| program | `CORE_TOP_PROGRAM` (16 words, unchanged) |
| core | five-stage `risc18.core.Core`, clocked at 60.000 MHz |
| line | `OK retires=%04x cycles=%08x pc=%04x value=%05x\r\n` (53 bytes, 8N1) |
| baud | 115 200 from 60 MHz: divisor 521 → 115 163.15 baud, −320 ppm |
| pads | `clk` = P6 (25 MHz), `uart_tx` = P11, both LVCMOS33 |

## Simulation reference (the number the board is checked against)

`tests/test_hil.py` measures the same program twice: directly on
`risc18.core.Core` and through `Core` + `HilStatus` on one 60 MHz clock.

* retires: **13**
* reset-to-halt cycles: **26** (cycle 1 = first clock edge after reset falls,
  count = the cycle on which `o_halt` is first seen). The 13 retires span
  cycles 5..25, i.e. the **21 cycles / 1.615 CPI** quoted in
  `docs/RESULTS.md` — a different quantity, asserted in the same test so the
  two cannot be confused.
* last retire: pc = 0x000f, value = 0x00000

## Synthesis (`tools/synth.py --top risc18.tops:HilTop --name hil5 --freq 25 --seed 1`)

| resource | used | total |
|---|---:|---:|
| LUT4 | 2196 | 24288 |
| TRELLIS_FF | 782 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax **73.07 MHz** at the 60 MHz the PLL generates (met); critical path
  6.0 ns logic + 7.7 ns routing. nextpnr derives the CLKOP constraint from the
  PLL parameters and prints 60.0 MHz (plus 75.0 MHz for the three disabled CLKOS
  outputs, `f_VCO / 8`), so the verdict is a 60 MHz verdict — the same one the
  re-rendered `m9_full.md` now carries.

## On-board result — three configurations, same numbers

```
$ .venv/bin/python tools/hil_status.py --load build/synth/hil5-board/top.bit \
      --expect-retires 13 --expect-cycles 26 --expect-pc 0xf --expect-value 0
line: bytearray(b'OK retires=000d cycles=0000001a pc=000f value=00000\r\n')
  retires got 13 (0xd) want 13 (0xd) OK
  cycles  got 26 (0x1a) want 26 +-5.0% -> delta +0 (+0.00 %) OK
  pc      got 15 (0xf) want 15 (0xf) OK
  value   got 0 (0x0) want 0 (0x0) OK
PASS
```

| run | line received | retires | cycles | pc | value | exit |
|---|---|---:|---:|---|---:|---:|
| 1 (load) | `OK retires=000d cycles=0000001a pc=000f value=00000` | 13 | 26 | 0xf | 0 | 0 |
| 2 (load) | `OK retires=000d cycles=0000001a pc=000f value=00000` | 13 | 26 | 0xf | 0 | 0 |
| 3 (load) | `OK retires=000d cycles=0000001a pc=000f value=00000` | 13 | 26 | 0xf | 0 | 0 |

Each run is a fresh `openFPGALoader -b colorlight` of the SRAM bitstream, so
each run also shows the counters starting from the configuration reset: the
harness could only report 26 if `cycles` were cleared with the core and
counted from the release edge. Board versus simulation: **13/13 retires,
26/26 cycles (0.00 %), pc 0xf, value 0** — an exact match, not a 5 % one.

## The PLL: why the ecppll set, and what stage 4 changed

With `FEEDBK_PATH="CLKOP"` the feedback divider counts **CLKOP**, not the VCO,
so the loop locks `f_out = f_in * CLKFB / CLKI` and `CLKOP_DIV` sets the VCO.
The version of `risc18.pll.Pll` this stage started from divided by `CLKOP_DIV`
a second time (`f_out = f_in * CLKFB / (CLKI * CLKOP)`), which is what the
measurements here ran into:

* `nextpnr-ecp5` on that class's default (1, 24, 10) printed
  `Derived VCO frequency 6024.1 MHz ... out of legal range [400MHz, 800MHz]`
  and constrained the CLKOP net to 600.2 MHz — a 600 MHz output from a 6 GHz
  VCO, exactly as the loop equation says.
* A heartbeat design clocked by it emitted nothing at all on the board
  (measured: 0 bytes in 15 s at 115 200; the same heartbeat clocked from the
  25 MHz pad streams its message, so the pin, the pad clock and the UART were
  not the problem).
* `ecppll -i 25 -o 60` gives CLKI_DIV=5, CLKFB_DIV=12, CLKOP_DIV=10 (PFD 5 MHz,
  VCO 600 MHz) with `CLKOP_CPHASE=4`, `CLKFB` tied to CLKOP and `ENCLKOP` low;
  that instance streams at 11.5 kB/s in the same heartbeat test. Changing only
  the dividers and keeping `CPHASE=0`/`CLKFB=0` was not enough on this board
  (the line came out stuck), so the vendor's phase and feedback wiring is part
  of the recipe; dropping the `ICP_CURRENT`/`LPF_RESISTOR`/`MFG_*` attributes
  made no difference.
* The 5 MHz PFD looked like the blocker because the class had a 10 MHz *PFD*
  floor. That floor was a misreading: 10 MHz is the datasheet's lower bound for
  the output/feedback clock, which here is CLKOP at 60 MHz; `ecppll` itself
  allows 3.125 MHz at the PFD.

Stage 3 therefore shipped `risc18.hil.HilPll`, a verbatim copy of the vendor
instance, so this harness could run while `pll.py` was left alone. **Stage 4
fixed the class instead**: `risc18.pll.Pll` now ships the 5/12/10/4 set with
CLKFB tied to CLKOP, validates it against `ecppll`'s bands, rejects the old
(1, 24, 10) set, and `HilTop` uses it — `HilPll` is gone. The numbers above did
not move, because the same cell is instantiated either way; the synthesis row
and the board run were repeated after the switch:

```
top | risc18.tops:HilTop (Pll, not HilPll)
resources | 2196 LUT4, 782 FF, 2 DP16KD, 1 MULT18X18D, 1 EHXPLLL
derived constraint | 60.0 MHz for boot_clk (no out-of-range VCO line)
fmax | 73.07 MHz (met 60.0), 6.0 ns logic + 7.7 ns routing
line: bytearray(b'OK retires=000d cycles=0000001a pc=000f value=00000\r\n')
```

`openFPGALoader -b colorlight build/synth/hil5-board/top.bit` (SRAM, no `-f`);
`tools/hil_status.py` reports PASS on all four fields.

## Reproducing the on-board run

```bash
# 1. metrics + fmax (pads unconstrained, full port list, as the other tops)
.venv/bin/python tools/synth.py --top risc18.tops:HilTop --name hil5 --freq 25 --seed 1

# 2. SRAM bitstream with the two pads pinned (clk P6, uart_tx P11).  The same
#    RTL, converted with only those two ports so no other FPGA pin, in
#    particular not M13 (the USB-UART's TX), is driven.
#    top.lpf:
#      LOCATE COMP "clk" SITE "P6";      IOBUF PORT "clk" IO_TYPE=LVCMOS33;
#      LOCATE COMP "uart_tx" SITE "P11"; IOBUF PORT "uart_tx" IO_TYPE=LVCMOS33;
#      FREQUENCY NET "clk" 25 MHz;
yosys -q -p 'read_verilog build/synth/hil5-board/top.v; synth_ecp5 -top top -json build/synth/hil5-board/top.json'
nextpnr-ecp5 --json build/synth/hil5-board/top.json --lpf build/synth/hil5-board/top.lpf \
    --25k --package CABGA256 --speed 6 --freq 25 --seed 1 --timing-allow-fail \
    --lpf-allow-unconstrained --placer heap --router router2 \
    --textcfg build/synth/hil5-board/top.config
ecppack --bootaddr 0 build/synth/hil5-board/top.config build/synth/hil5-board/top.bit

# 3. load into SRAM (never -f) and check the line
.venv/bin/python tools/hil_status.py --load build/synth/hil5-board/top.bit \
    --expect-retires 13 --expect-cycles 26 --expect-pc 0xf --expect-value 0
```

`openFPGALoader -b colorlight --detect` reports `idcode 0x41111043`,
`family ECP5`, `model LFE5U-25` on the board this ran against.
