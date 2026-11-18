#!/bin/bash
# 修复前后对照：同一模型、同一时间窗**逐块交错**跑两套题库（README §31）。
#
# 为什么需要这一轮：§30.3 修掉了两道题自相矛盾（`ecom-02` 题面说"周期末取消"、
# 判据要"立刻终止"；`dev-14` 题面禁止的动作恰是唯一解）。修完之后那两题的
# 通过率从 0% / 6.7% 跳上去是**必然的**，但"修两题会不会动别的地方"没人量过。
# 而且修复改的是判据集，所以任何跨轮比较都得先有同批同模型的对照臂。
#
# 两臂：BASE=3852029 的题库（修复前）vs 工作区题库（修复后）。两套题的差别
# 只有那两道 —— 这个前提由驱动脚本自己验（`[diff]` 行），不靠人记。
#
# 用法：bash scripts/prefix_pair_scan.sh
#   MODEL=chatanywhere/deepseek-v4.1-flash RUNS=2 CHUNKS=5 BASE=3852029
# 产物：demo/fixpair_{before,after}_chunk_0N.json → 合并成 demo/fixpair_{before,after}.json
# 宿主重启后按「文件已存在」跳过，直接再跑一次即可续上。
set -u
cd "$(dirname "$0")/.." || exit 1
MODEL="${MODEL:-chatanywhere/deepseek-v4.1-flash}"
RUNS="${RUNS:-2}"
CHUNKS="${CHUNKS:-5}"
BASE="${BASE:-3852029}"
WORK="${WORK:-/tmp/arga_fix_pair}"

mkdir -p "$WORK/chunks"
python3 - "$CHUNKS" "$WORK/chunks" <<'PY'
import pathlib, sys
n = int(sys.argv[1]); out = pathlib.Path(sys.argv[2])
ids = sorted(p.stem for p in pathlib.Path("scenarios").glob("*.json"))
for i in range(n):
    (out / f"fix_chunk_{i:02d}").write_text(",".join(ids[i::n]))
print(f"{len(ids)} scenarios -> {n} chunks")
PY

# 修复前的题库：从 BASE 提交里取出来，和当前题库并排放着（工作区不动）
rm -rf "$WORK/scen_before"; mkdir -p "$WORK/scen_before"
git archive "$BASE" scenarios | tar -x -C "$WORK/scen_before" --strip-components=1
cp -r scenarios/. "$WORK/scen_after_tmp" 2>/dev/null || true
rm -rf "$WORK/scen_after"; mkdir -p "$WORK/scen_after"; cp scenarios/*.json "$WORK/scen_after"/
rm -rf "$WORK/scen_after_tmp"

# 自证两套题只差那两道修掉的题 —— 否则"前后差异"就不只是修复的效果
python3 - "$WORK" <<'PY'
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
before = {p.stem: json.loads(p.read_text()) for p in (work / "scen_before").glob("*.json")}
after = {p.stem: json.loads(p.read_text()) for p in (work / "scen_after").glob("*.json")}
assert set(before) == set(after), "两套题库的题目集合不同，对照不成立"
diff = sorted(k for k in before if before[k] != after[k])
print(f"[diff] 两套题库共 {len(before)} 题，内容不同 {len(diff)} 题：{diff}")
PY

# 交错：同一块的 before/after 相邻跑完才进下一块，模型漂移被压在块内
for c in "$WORK"/chunks/fix_chunk_*; do
  k=$(basename "$c")
  ids=$(cat "$c")
  for arm in before after; do
    out="demo/fixpair_${arm}_${k}.json"
    if [ -s "$out" ]; then echo "[skip] $out $(date -Is)"; continue; fi
    echo "[run] $out $(date -Is)"
    ARGA_SCEN_DIR="$WORK/scen_$arm" \
      python3 -u demo/agent_compare.py --models "$MODEL" --runs "$RUNS" --workers 4 \
        --only "$ids" --out "$out" >> "$WORK/arm_${arm}.log" 2>&1
    echo "[done] $out rc=$? $(date -Is)"
  done
done

python3 - <<'PY'
import glob, json, pathlib
for arm in ("before", "after"):
    rows = []
    for f in sorted(glob.glob(f"demo/fixpair_{arm}_fix_chunk_*.json")):
        rows += json.load(open(f))
    pathlib.Path(f"demo/fixpair_{arm}.json").write_text(json.dumps(rows))
    print(f"merged {arm}: {len(rows)} episodes")
PY
echo "[ALL DONE] $(date -Is)"
