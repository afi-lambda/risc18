# m4_fetch

top: `risc18.tops:FetchTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 37 | 24288 |
| TRELLIS_FF | 99 | 24288 |
| DP16KD | 1 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 150.2 MHz (constraint 60.0 MHz, met)

* critical path: logic 5.6 ns, routing 1.0 ns
