# m3_branch_target

top: `risc18.tops:BranchTargetTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 48 | 24288 |
| TRELLIS_FF | 65 | 24288 |
| DP16KD | 0 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 215.52 MHz (constraint 60.0 MHz, met)

* critical path: logic 1.9 ns, routing 2.7 ns
