# m6_core

top: `risc18.tops:CoreTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1908 | 24288 |
| TRELLIS_FF | 584 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 49.12 MHz (constraint 60.0 MHz, missed)

* critical path: logic 4.8 ns, routing 15.5 ns
