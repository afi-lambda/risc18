#!/usr/bin/env bash
# Structural ablations for the cost breakdown in docs/ARCHITECTURE.md.
#
# Each variant is a scratch copy of the tree with ONE datapath cone removed or
# tied off, synthesised with tools/synth.py at CoreTop seed 1.  These are
# synthesis-only builds: they are functionally wrong on purpose, no test ever
# runs against them, and nothing is programmed.
#
#   tools/ablations.sh                 # all variants, flop register file
#   tools/ablations.sh --ram           # all variants, --ram-rf
#   tools/ablations.sh nobyp nocmp     # named variants only
#
# Output: one line per variant, then a table.  Builds land in
# ${ABL_DIR:-build/ablations}.
#
# Reading the numbers: LUT4 attribution by subtraction is only meaningful for
# structural removals of a whole cone.  Small edits let ABC remap globally and
# can *increase* LUT4 while removing logic (see the `nomul` row: the DSP leaves
# the critical loop, AAC re-optimises the x_value cone for speed and area goes
# up).  Quote the cell-type counts and the flag flip for hard numbers; use these
# for the shape of the cost.
set -eu
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ABL_DIR=${ABL_DIR:-$REPO/build/ablations}
PY=${PY:-$REPO/.venv/bin/python}
RAM=0
VARIANTS=()
for a in "$@"; do
  case "$a" in
    --ram) RAM=1 ;;
    --help|-h) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) VARIANTS+=("$a") ;;
  esac
done
[ ${#VARIANTS[@]} -eq 0 ] && VARIANTS=(ctl nobyp nocmp notgt nomul)

suffix=""
[ "$RAM" -eq 1 ] && suffix="_ram"
mkdir -p "$ABL_DIR"

patch_core() {  # patch_core <variant> <core.py>
  "$PY" - "$1" "$2" <<'PY'
import pathlib, sys
variant, path = sys.argv[1], pathlib.Path(sys.argv[2])
s = path.read_text()
before = s
if variant == "nomul":
    s = s.replace("            m.d.comb += x_value.eq((dx_a * dx_b)[:isa.WIDTH])",
                  "            m.d.comb += x_value.eq(0)")
elif variant == "notgt":
    s = s.replace("            ct_target.eq(target.o_target),",
                  "            ct_target.eq(dx_pc),")
elif variant == "nocmp":
    s = s.replace("            branch.i_a.eq(dx_a),", "            branch.i_a.eq(0),")
    s = s.replace("            branch.i_b.eq(dx_rs2),", "            branch.i_b.eq(0),")
elif variant == "nobyp":
    # flop form: regfile read -> x/m bypass
    s = s.replace("""                d_rs1.eq(Mux(x_hit1, x_value,
                             Mux(m_hit1, m_value, regfile.o_rs1))),""",
                  "                d_rs1.eq(regfile.o_rs1),")
    s = s.replace("""                d_rs2.eq(Mux(x_hit2, x_value,
                             Mux(m_hit2, m_value, regfile.o_rs2))),""",
                  "                d_rs2.eq(regfile.o_rs2),")
    # RAM form: regfile read -> x/m/w/wq bypass
    for reg in ("1", "2"):
        s = s.replace(f"""                d_rs{reg}.eq(Mux(x_hit{reg}, x_value,
                             Mux(m_hit{reg}, m_value,
                                 Mux(w_hit{reg}, w_value,
                                     Mux(wq_hit{reg}, wq_value,""",
                      f"""                d_rs{reg}.eq(Mux(0, x_value,
                             Mux(0, m_value,
                                 Mux(0, w_value,
                                     Mux(0, wq_value,""")
elif variant != "ctl":
    sys.exit(f"unknown variant {variant}")
if s == before and variant != "ctl":
    sys.exit(f"{variant}: patch applied nothing (source shape changed?)")
path.write_text(s)
PY
}

printf '%-10s %8s %8s %10s\n' variant LUT4 FF fmax
printf '%-10s %8s %8s %10s\n' -------- ---- -- ----
for v in "${VARIANTS[@]}"; do
  name="$v$suffix"
  dir="$ABL_DIR/$name"
  rm -rf "$dir"
  rsync -a --exclude build --exclude build/ --exclude .git "$REPO/" "$dir/"
  [ "$v" = "ctl" ] || patch_core "$v" "$dir/src/risc18/core.py"
  extra=()
  [ "$RAM" -eq 1 ] && extra+=(--ram-rf)
  ( cd "$dir" && "$PY" tools/synth.py --top risc18.tops:CoreTop --name "$name" \
      --freq 60 --seed 1 --out build "${extra[@]}" >/dev/null )
  "$PY" - "$dir/build/metrics/$name.json" "$name" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
f = d["fmax"][0]["fmax_mhz"] if d.get("fmax") else float("nan")
print(f"{sys.argv[2]:<10} {d['util']['LUT4']['used']:8d} "
      f"{d['util']['TRELLIS_FF']['used']:8d} {f:10.2f}")
PY
done
