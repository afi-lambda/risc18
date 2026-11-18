# m12_full6_s1

top: `risc18.tops:Full6Top`  
device: `LFE5U-25F-6CABGA256`  
constraint: 25.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1220 | 24288 |
| TRELLIS_FF | 490 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$boot_clk`: 78.35 MHz (constraint 120.0 MHz, missed) -- nextpnr's derived constraint for the PLL-generated clock (computed from the PLL parameters, not from --freq); the PLL's CLKOP is 120.000 MHz

* critical path: logic 3.0 ns, routing 9.8 ns
