# m7_core

top: `risc18.tops:CoreTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 2265 | 24288 |
| TRELLIS_FF | 584 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 46.55 MHz (constraint 60.0 MHz, missed)

* critical path: logic 4.6 ns, routing 16.9 ns
