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
  6.0 ns logic + 7.7 ns routing. nextpnr derives the CLKOP constraint itself
  from the PLL parameters — with the ecppll set below it derives 60.0 MHz, so
  the "met/missed" verdict is finally meaningful (the committed `m9_full.md`
  rows compare against a derived 600.24 MHz, see the PLL note).

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

## The PLL: `risc18.pll.Pll` does not produce 60 MHz on this board

`:class:`risc18.pll.Pll`` divides twice — `f_out = f_in * CLKFB / (CLKI *
CLKOP)` — but with `FEEDBK_PATH="CLKOP"` the feedback divides **CLKOP** down to
the PFD, so the loop locks `f_out = f_in * CLKFB / CLKI` and `CLKOP_DIV` sets
the VCO. Consequences, all observed here:

* `nextpnr-ecp5` on the `Pll()` default (1, 24, 10) prints
  `Derived VCO frequency 6024.1 MHz ... out of legal range [400MHz, 800MHz]`
  and constrains the CLKOP net to 600.2 MHz — a 600 MHz output from a 6 GHz VCO.
* A heartbeat design clocked by `Pll()` emits nothing at all on the board
  (measured: 0 bytes in 15 s at 115 200; the same heartbeat clocked from the
  25 MHz pad streams its message, so the pin, the pad clock and the UART are
  not the problem).
* `ecppll -i 25 -o 60` gives CLKI_DIV=5, CLKFB_DIV=12, CLKOP_DIV=10 (PFD 5 MHz,
  VCO 600 MHz) with `CLKOP_CPHASE=4` and `CLKFB` tied to CLKOP; that instance
  streams at 11.5 kB/s in the same heartbeat test, and `nextpnr` now derives
  60.0 MHz. Changing only the dividers and keeping `CPHASE=0`/`CLKFB=0` was not
  enough on this board (the line came out stuck), so the vendor's phase and
  feedback wiring is part of the recipe; dropping the
  `ICP_CURRENT`/`LPF_RESISTOR`/`MFG_*` attributes made no difference.
* The 5 MHz PFD is below the 10 MHz floor `Pll()` enforces, which is why the
  correct set cannot be expressed through that class.

`HilPll` in `src/risc18/hil.py` is therefore the ecppll instance verbatim, and
`HilTop` uses it. `FullTop`, `CoreTop`, `pll.py` and their committed metrics are
untouched: this note records the measurement, it does not change those tops.

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
