# m4_imem

top: `risc18.tops:InstrMemoryTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 5 | 24288 |
| TRELLIS_FF | 36 | 24288 |
| DP16KD | 1 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 149.93 MHz (constraint 60.0 MHz, met)

* critical path: logic 5.6 ns, routing 1.1 ns
