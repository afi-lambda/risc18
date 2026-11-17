# m9_core

top: `risc18.tops:CoreTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1829 | 24288 |
| TRELLIS_FF | 605 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 76.4 MHz (constraint 60.0 MHz, met)

* critical path: logic 6.0 ns, routing 7.1 ns
