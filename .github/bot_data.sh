#!/usr/bin/env bash
# S4（docs/infra/02-S4機器人分離規格.md）：機器人的狀態檔放在 bot-data 分支，不再寫進 main
#   bash .github/bot_data.sh load 檔案...          → 從 bot-data 拿最新的檔案放到工作目錄（bot-data 沒有就不動）
#   bash .github/bot_data.sh save "說明" 檔案...   → 把這些檔案寫回 bot-data（整個檔案覆蓋，失敗就重拿最新再試）
# 不碰 main；main 有規則擋住機器人推送。
set -euo pipefail
BR=bot-data
cmd=$1; shift

fetch() { git fetch -q --depth=1 origin "+refs/heads/$BR:refs/remotes/origin/$BR"; }

if [ "$cmd" = load ]; then
  fetch || { echo "::warning title=bot-data::拿不到 $BR 分支"; exit 0; }
  for f in "$@"; do
    if git cat-file -e "origin/$BR:$f" 2>/dev/null; then
      mkdir -p "$(dirname "$f")"
      git show "origin/$BR:$f" > "$f"
    fi
  done
  exit 0
fi

[ "$cmd" = save ] || { echo "用法：load|save"; exit 2; }
msg=$1; shift
root=$(pwd)
for i in 1 2 3 4 5; do
  fetch
  tmp=$(mktemp -d)
  git worktree add -q --detach "$tmp" "origin/$BR"
  for f in "$@"; do
    [ -f "$root/$f" ] || continue
    mkdir -p "$tmp/$(dirname "$f")"
    cp "$root/$f" "$tmp/$f"
    git -C "$tmp" add "$f"
  done
  if git -C "$tmp" diff --cached --quiet; then
    git worktree remove --force "$tmp"; echo "沒有變更"; exit 0
  fi
  git -C "$tmp" -c user.name="github-actions[bot]" -c user.email="41898+github-actions[bot]@users.noreply.github.com" \
    commit -q -m "$msg"
  if git -C "$tmp" push -q origin "HEAD:refs/heads/$BR"; then
    git worktree remove --force "$tmp"; exit 0
  fi
  git worktree remove --force "$tmp"
  sleep $((i * 3))
done
echo "::error title=bot-data::推不上去"
exit 1
