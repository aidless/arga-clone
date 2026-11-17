#!/bin/bash
# 地板全量对照：floor=12 vs 8，同路由、同时间窗**逐块交错**跑（README §29.10）。
#
# 为什么交错：上一版把整臂 floor=12 跑完再跑整臂 floor=8（README §29.9），六小时里
# 上游换了两次状态，两臂的差异根本分不清是地板还是上游。交错后同一块的两次跑相隔
# 只有几分钟，模型漂移被压在块内。
#
# 用法：bash scripts/floor_pair_scan.sh
#   MODEL=sidrune/deepseek-v4-1-flash RUNS=2 CHUNKS=5
# 产物：demo/floorscan_{12,8}_floor_chunk_0N.json → 合并成 demo/floorscan_{12,8}.json
# 宿主重启后按「文件已存在」跳过，所以可以直接再跑一次续上。
set -u
cd "$(dirname "$0")/.." || exit 1
MODEL="${MODEL:-sidrune/deepseek-v4-1-flash}"
RUNS="${RUNS:-2}"
CHUNKS="${CHUNKS:-5}"
WORK="${WORK:-/tmp/arga_floor_pair}"

# 分块：按 scenario id 排序后均匀切开，保证两次跑看到的是同一批题。
mkdir -p "$WORK/chunks"
python3 - "$CHUNKS" "$WORK/chunks" <<'PY'
import pathlib, sys
n = int(sys.argv[1]); out = pathlib.Path(sys.argv[2])
ids = sorted(p.stem for p in pathlib.Path("scenarios").glob("*.json"))
for i in range(n):
    (out / f"floor_chunk_{i:02d}").write_text(",".join(ids[i::n]))
print(f"{len(ids)} scenarios -> {n} chunks")
PY

# 两臂的题库（各自按自己的地板重建一次，工作区 scenarios/ 不动）
for floor in 12 8; do
  dst="$WORK/scen_floor$floor"
  mkdir -p "$dst"; cp scenarios/*.json "$dst"/
  ARGA_BUDGET_FLOOR="$floor" ARGA_SCEN_OUT="$dst" python3 scenarios/build_suite.py >/dev/null || exit 1
  ARGA_BUDGET_FLOOR="$floor" ARGA_SCEN_OUT="$dst" python3 scenarios/build_suite.py --check >/dev/null || exit 1
done

# 交错：每一块的 12 与 8 相邻跑完才进下一块。
for c in "$WORK"/chunks/floor_chunk_*; do
  k=$(basename "$c")
  ids=$(cat "$c")
  for floor in 12 8; do
    out="demo/floorscan_${floor}_${k}.json"
    if [ -s "$out" ]; then echo "[skip] $out $(date -Is)"; continue; fi
    echo "[run] $out $(date -Is)"
    ARGA_SCEN_DIR="$WORK/scen_floor$floor" ARGA_BUDGET_FLOOR="$floor" \
      python3 -u demo/agent_compare.py --models "$MODEL" --runs "$RUNS" --workers 4 \
        --only "$ids" --out "$out" >> "$WORK/arm_${floor}.log" 2>&1
    echo "[done] $out rc=$? $(date -Is)"
  done
done

python3 - <<'PY'
import glob, json, pathlib
for floor in (12, 8):
    rows = []
    for f in sorted(glob.glob(f"demo/floorscan_{floor}_floor_chunk_*.json")):
        rows += json.load(open(f))
    pathlib.Path(f"demo/floorscan_{floor}.json").write_text(json.dumps(rows))
    print(f"merged {floor}: {len(rows)} episodes")
PY
echo "[ALL DONE] $(date -Is)"
