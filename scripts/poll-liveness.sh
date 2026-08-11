#!/bin/bash
# poll-liveness.sh — 背景任務三訊號 liveness poll（成功 / 死亡 / 疑似卡住）
#
# 設計不變量（源自 /pr-review 一次 codex 靜默死亡、被空等 38min 的事故）：
#   ① 成功訊號（產出物含 success pattern）→ 終局，exit 0
#   ② process 消失（pgrep 空）→ 終局，exit 2（token 已沉沒、retry 是唯一選項）
#   ③ 產出物 mtime idle → 只觸發「去查 ②」與上報，本 script 絕不 kill；
#      idle 且 process 活著 = 可能在長 reasoning（token_count 是 per-turn 寫入），誤殺才是雙倍 token
#   產出物定位用內容（workdir 字串）不用 session id——codex wrapper 與主 session id 前綴
#   不保證相同、多 session 環境全域 glob 會撈到別人的 rollout（假活著訊號）
#
# 用法 1：定位 codex rollout（回傳所有匹配、含 wrapper——poll 模式吃多檔自動解歧義）
#   poll-liveness.sh find-rollout <workdir> [<since-epoch>]
#
# 用法 2：poll（單輪 ≤ deadline 秒、配合 CC Bash tool 10min 硬上限預設 540）
#   poll-liveness.sh poll --pgrep <pattern> --success <grep-pattern> \
#     [--idle 300] [--stuck 900] [--deadline 540] <artifact-file...>
#   exit 0 = DONE（任一檔含 success pattern）
#   exit 1 = STILL_RUNNING（deadline 到、訊號健康）→ 呼叫端下一輪 Bash call 續 poll
#   exit 2 = DEAD（process 消失且無 success）→ 呼叫端 retry
#   exit 3 = STUCK_SUSPECT（idle > stuck 且 process 活著）→ 上報使用者拍板；kill 權限在呼叫端

set -u

mode="${1:-}"; shift || true

newest_mtime() {
  local m=0 f t
  for f in "$@"; do
    [ -f "$f" ] || continue
    t=$(stat -f%m "$f" 2>/dev/null || echo 0)
    [ "$t" -gt "$m" ] && m=$t
  done
  echo "$m"
}

case "$mode" in
  find-rollout)
    workdir="${1:?usage: find-rollout <workdir> [<since-epoch>]}"
    since="${2:-0}"
    found=0
    for f in "$HOME"/.codex/sessions/*/*/*/rollout-*.jsonl; do
      [ -f "$f" ] || continue
      [ "$(stat -f%m "$f")" -ge "$since" ] || continue
      if head -5 "$f" | grep -qF "$workdir"; then
        echo "$f"; found=1
      fi
    done
    [ "$found" -eq 1 ] || { echo "NO_ROLLOUT_FOUND for $workdir (since=$since)" >&2; exit 1; }
    ;;

  poll)
    idle=300; stuck=900; deadline=540; pgrep_pat=""; success_pat=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --pgrep) pgrep_pat="$2"; shift 2;;
        --success) success_pat="$2"; shift 2;;
        --idle) idle="$2"; shift 2;;
        --stuck) stuck="$2"; shift 2;;
        --deadline) deadline="$2"; shift 2;;
        *) break;;
      esac
    done
    [ -n "$pgrep_pat" ] && [ -n "$success_pat" ] && [ $# -ge 1 ] || {
      echo "usage: poll --pgrep <pat> --success <pat> [--idle N] [--stuck N] [--deadline N] <file...>" >&2; exit 64; }

    end=$(( $(date +%s) + deadline ))
    warned=0
    while [ "$(date +%s)" -lt "$end" ]; do
      if grep -q "$success_pat" "$@" 2>/dev/null; then
        echo "DONE at $(date '+%H:%M:%S')"; exit 0
      fi
      alive=0; pgrep -f "$pgrep_pat" >/dev/null 2>&1 && alive=1
      if [ "$alive" -eq 0 ]; then
        sleep 2  # process 剛結束可能還在 flush，緩衝後再驗一次成功訊號
        if grep -q "$success_pat" "$@" 2>/dev/null; then
          echo "DONE (process exited) at $(date '+%H:%M:%S')"; exit 0
        fi
        echo "DEAD — process gone, no success marker, at $(date '+%H:%M:%S')"; exit 2
      fi
      age=$(( $(date +%s) - $(newest_mtime "$@") ))
      if [ "$age" -gt "$stuck" ]; then
        echo "STUCK_SUSPECT — idle ${age}s, process alive; NOT killing (呼叫端上報使用者)"; exit 3
      fi
      if [ "$age" -gt "$idle" ] && [ "$warned" -eq 0 ]; then
        echo "warn: artifact idle ${age}s, process alive — 可能長 reasoning，繼續等"
        warned=1
      fi
      sleep 10
    done
    echo "STILL_RUNNING — deadline reached, signals healthy (idle $(( $(date +%s) - $(newest_mtime "$@") ))s)"
    exit 1
    ;;

  *)
    echo "usage: poll-liveness.sh find-rollout|poll ..." >&2; exit 64
    ;;
esac
