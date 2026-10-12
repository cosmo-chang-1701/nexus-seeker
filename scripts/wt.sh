#!/usr/bin/env bash
# Nexus Seeker Git Worktree Helper Script
set -e

# 定位專案容器根目錄
# 若在 main/ 或 feature/ worktree 內，上一層為容器根目錄；若在根目錄則當前為根目錄
if [ -f ".git" ] && grep -q "gitdir: ./.bare" .git 2>/dev/null; then
  REPO_ROOT="$(pwd)"
elif [ -f "../.git" ] && grep -q "gitdir: ./.bare" ../.git 2>/dev/null; then
  REPO_ROOT="$(cd .. && pwd)"
elif [ -d ".bare" ]; then
  REPO_ROOT="$(pwd)"
elif [ -d "../.bare" ]; then
  REPO_ROOT="$(cd .. && pwd)"
else
  # 傳統模式或 fallback：使用 git rev-parse
  TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  if [[ "$TOPLEVEL" == */.worktrees/* ]]; then
    REPO_ROOT="$(cd "$TOPLEVEL/../.." && pwd)"
  else
    REPO_ROOT="$TOPLEVEL"
  fi
fi

command="$1"
shift || true

case "$command" in
  list)
    git worktree list
    ;;
  add)
    BRANCH="$1"
    if [ -z "$BRANCH" ]; then
      echo "用法: ./scripts/wt.sh add <branch-name> [base-branch]"
      exit 1
    fi
    TARGET_DIR="$REPO_ROOT/$BRANCH"
    BASE="${2:-origin/main}"
    echo "🌿 正在建立 Worktree: $TARGET_DIR (分支: $BRANCH, 基準: $BASE)..."
    git worktree add -b "$BRANCH" "$TARGET_DIR" "$BASE"

    # 自動複製環境變數設定
    if [ -f "$REPO_ROOT/main/nexus_core/.env" ]; then
      echo "🔑 複製 nexus_core/.env 至 $BRANCH..."
      cp "$REPO_ROOT/main/nexus_core/.env" "$TARGET_DIR/nexus_core/.env"
    elif [ -f "$REPO_ROOT/nexus_core/.env" ]; then
      echo "🔑 複製 nexus_core/.env 至 $BRANCH..."
      cp "$REPO_ROOT/nexus_core/.env" "$TARGET_DIR/nexus_core/.env"
    fi
    if [ -f "$REPO_ROOT/main/nexus_edge_scraper/.env" ]; then
      echo "🔑 複製 nexus_edge_scraper/.env 至 $BRANCH..."
      cp "$REPO_ROOT/main/nexus_edge_scraper/.env" "$TARGET_DIR/nexus_edge_scraper/.env"
    elif [ -f "$REPO_ROOT/nexus_edge_scraper/.env" ]; then
      echo "🔑 複製 nexus_edge_scraper/.env 至 $BRANCH..."
      cp "$REPO_ROOT/nexus_edge_scraper/.env" "$TARGET_DIR/nexus_edge_scraper/.env"
    fi
    echo "✅ Worktree $BRANCH 建立完成！請進入目錄: cd $BRANCH"
    ;;
  remove)
    BRANCH="$1"
    if [ -z "$BRANCH" ]; then
      echo "用法: ./scripts/wt.sh remove <branch-name>"
      exit 1
    fi
    TARGET_DIR="$REPO_ROOT/$BRANCH"
    if [ "$BRANCH" = "main" ]; then
      echo "❌ 禁止移除 main 工作區！"
      exit 1
    fi
    echo "🗑️ 正在移除 Worktree: $TARGET_DIR..."
    git worktree remove "$TARGET_DIR"
    git worktree prune
    echo "✅ Worktree $BRANCH 已安全移除。"
    ;;
  *)
    echo "🌌 Nexus Seeker Worktree CLI 管理工具"
    echo "指令:"
    echo "  ./scripts/wt.sh list                  列出所有 active worktrees"
    echo "  ./scripts/wt.sh add <branch> [base]   新增工作區並自動複製 .env"
    echo "  ./scripts/wt.sh remove <branch>       移除工作區並清理 refs"
    ;;
esac
