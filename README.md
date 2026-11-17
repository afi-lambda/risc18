# risc18 — 18-bit RISC for the Lattice ECP5

A small Harvard RISC for the Colorlight 5A-75B (LFE5U-25F): 18-bit data and
instructions, 16 registers, 16-bit word addresses, one EBR for instructions and
one for data, three-to-five stage pipeline, DSP multiplier.

The architecture and milestone plan come from `design.txt`; the executable
contract is [docs/ISA.md](docs/ISA.md) plus `src/risc18/isa.py`.

## Quick start

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m pytest tests/ -q
```

Synthesis / timing for any top that exposes `.ports`:

```bash
.venv/bin/python tools/synth.py --top risc18.tops:CoreTop --name m7_core --freq 60
```

Artifacts land in `build/metrics/<name>.json` and `.md`; committed summaries
live in `docs/metrics/`. Device: LFE5U-25F-6CABGA256 (Colorlight 5A-75B).

## Layout

```
src/risc18/isa.py       frozen encodings + reference decode
src/risc18/golden.py    instruction-accurate golden model
src/risc18/decoder.py   M0 Amaranth decoder
src/risc18/regfile.py   M1 register file
src/risc18/alu.py       M2 ALU, branch.py comparison unit
src/risc18/pc.py        M3 PC / redirect unit
src/risc18/imem.py      M4 instruction EBR + fetch
src/risc18/core.py      M5+ CPU, M6+ data memory, M7 forwarding, M8 MUL
tools/asm.py            two-pass assembler for test programs
tools/synth.py          Amaranth -> Verilog -> yosys -> nextpnr -> metrics
docs/ISA.md             normative ISA and interface contract
docs/MILESTONES.md      milestone scopes and gates
sw/                     assembly test programs
tests/                  pytest suites (Amaranth simulation + golden lockstep)
```

## Status

| Milestone | State |
|---|---|
| Scaffold: ISA contract, golden model, assembler, synth metrics | done |
| M0 decoder | done |
| M1 register file | done |
| M2 ALU + branch compare | done |
| M3 PC and branches | done |
| M4 instruction EBR fetch | pending |
| M5 core without data memory | pending |
| M6 data EBR and LD/ST | pending |
| M7 forwarding and hazards | pending |
| M8 DSP multiplier | pending |
| M9 timing and resource optimization | pending |
