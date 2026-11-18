# m11_core_flop (seed 1, flop register file)

top: `risc18.tops:CoreTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1640 | 24288 |
| TRELLIS_FF | 640 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 71.03 MHz (constraint 60.0 MHz, met)

* critical path: logic 5.8 ns, routing 8.3 ns
