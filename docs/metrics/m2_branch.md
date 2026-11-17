# m2_branch

top: `risc18.tops:BranchTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 57 | 24288 |
| TRELLIS_FF | 39 | 24288 |
| DP16KD | 0 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 184.03 MHz (constraint 60.0 MHz, met)

* critical path: logic 2.4 ns, routing 3.1 ns
