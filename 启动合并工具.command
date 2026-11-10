#!/bin/zsh
set -e
cd -- "${0:A:h}"
echo 'Worktree Merge · 本地合并工作台'
echo '请粘贴仓库或任一 worktree 的完整路径（不需要引号），然后回车：'
IFS= read -r repo_path
if [[ -z "$repo_path" ]]; then
  echo '未输入路径，已退出。'
  exit 1
fi
exec python3 ./server.py --repo "$repo_path"
