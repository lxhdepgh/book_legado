#!/usr/bin/env bash
# 分片跑全库差分：单源触发 native 崩溃只会损失一个分片。
set -u
cd "$(dirname "$0")/../.."
PY=/d/env_manger/minicaoda/envs/legodo_booksource_check/python.exe
SIZE=${SIZE:-100}
TOTAL=${TOTAL:-1100}
mkdir -p tools/legado_diff/chunks
for ((off = 0; off < TOTAL; off += SIZE)); do
  log=tools/legado_diff/chunks/chunk_$off.log
  echo "=== chunk offset=$off ==="
  "$PY" -u -X faulthandler tools/legado_diff/run_diff.py \
      --offset "$off" --limit "$SIZE" --concurrency 4 > "$log" 2>&1
  echo "exit=$? $(grep -m1 '^SUMMARY ' "$log" || tail -2 "$log" | head -1)"
done
