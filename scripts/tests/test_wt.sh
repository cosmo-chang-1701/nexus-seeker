#!/usr/bin/env bash
# Nexus Seeker Worktree Status Test Suite
set -euo pipefail

# ANSI 顏色設定
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT_SCRIPT_SOURCE="$(cd "$SCRIPT_DIR/.." && pwd)/wt.sh"

if [ ! -f "$WT_SCRIPT_SOURCE" ]; then
  echo -e "${RED}錯誤: 找不到 wt.sh 腳本 ($WT_SCRIPT_SOURCE)${NC}"
  exit 1
fi

TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

pass() {
  PASSED_TESTS=$((PASSED_TESTS + 1))
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  echo -e "  [${GREEN}PASS${NC}] $1"
}

fail() {
  FAILED_TESTS=$((FAILED_TESTS + 1))
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  echo -e "  [${RED}FAIL${NC}] $1: $2"
}

# 建立獨立沙盒測試環境
SANDBOX="$(mktemp -d /tmp/nexus_wt_test_XXXXXX)"
cleanup() {
  rm -rf "$SANDBOX"
}
trap cleanup EXIT INT TERM

echo -e "${BLUE}================================================================${NC}"
echo -e "${BLUE}  🧪 Nexus Seeker Worktree Status 自動化測試套件${NC}"
echo -e "${BLUE}================================================================${NC}"
echo "測試沙盒路徑: $SANDBOX"

# 初始化 Bare Repo 與工作區
BARE_DIR="$SANDBOX/.bare"
MAIN_DIR="$SANDBOX/main"
DIRTY_DIR="$SANDBOX/dirty-wt"
CLEAN_DIR="$SANDBOX/clean-wt"
SCRIPTS_DIR="$SANDBOX/scripts"

mkdir -p "$BARE_DIR" "$SCRIPTS_DIR"
cp "$WT_SCRIPT_SOURCE" "$SCRIPTS_DIR/wt.sh"
chmod +x "$SCRIPTS_DIR/wt.sh"

# Git 初始配置
export GIT_AUTHOR_NAME="Nexus Test"
export GIT_AUTHOR_EMAIL="test@nexus-seeker.local"
export GIT_COMMITTER_NAME="Nexus Test"
export GIT_COMMITTER_EMAIL="test@nexus-seeker.local"

git init --bare "$BARE_DIR" >/dev/null
git -C "$BARE_DIR" config user.name "Nexus Test"
git -C "$BARE_DIR" config user.email "test@nexus-seeker.local"

# 建立 main 工作區
git -C "$BARE_DIR" worktree add -b main "$MAIN_DIR" >/dev/null 2>&1
echo "# Nexus Main" > "$MAIN_DIR/README.md"
git -C "$MAIN_DIR" add README.md
git -C "$MAIN_DIR" commit -m "Initial commit on main" >/dev/null 2>&1

# 在沙盒根目錄建立 .git 指向 .bare
echo "gitdir: ./.bare" > "$SANDBOX/.git"

# 建立 clean-wt 與 dirty-wt
git -C "$BARE_DIR" worktree add -b clean-branch "$CLEAN_DIR" >/dev/null 2>&1
git -C "$BARE_DIR" worktree add -b dirty-branch "$DIRTY_DIR" >/dev/null 2>&1

# 設定 dirty-wt 之變更（含 staged、unstaged、untracked）
echo "staged content" > "$DIRTY_DIR/staged.txt"
git -C "$DIRTY_DIR" add staged.txt

echo "unstaged append" >> "$DIRTY_DIR/README.md"

echo "untracked content" > "$DIRTY_DIR/untracked.txt"

# 切換至沙盒目錄執行所有測試
cd "$SANDBOX"

# -----------------------------------------------------------------------------
# 測試 1: Clean 工作區檢測 (test_clean_detection)
# -----------------------------------------------------------------------------
test_clean_detection() {
  local output
  output="$("$SCRIPTS_DIR/wt.sh" status)"
  if echo "$output" | grep -q "clean-wt" && echo "$output" | grep -q "CLEAN"; then
    pass "test_clean_detection: 乾淨工作區正確標記為 CLEAN"
  else
    fail "test_clean_detection" "未能正確檢測 clean-wt 為 CLEAN (輸出: $output)"
  fi
}

# -----------------------------------------------------------------------------
# 測試 2: Dirty 工作區分類與計數 (test_dirty_detection)
# -----------------------------------------------------------------------------
test_dirty_detection() {
  local output
  output="$("$SCRIPTS_DIR/wt.sh" status)"
  if echo "$output" | grep -q "dirty-wt" && echo "$output" | grep -q "DIRTY"; then
    if echo "$output" | grep -q "暫存: 1" && echo "$output" | grep -q "未暫存: 1" && echo "$output" | grep -q "未追蹤: 1"; then
      pass "test_dirty_detection: 變更工作區正確分類為 DIRTY 且計數精確 (暫存/未暫存/未追蹤)"
    else
      fail "test_dirty_detection" "DIRTY 計數不正確 (未包含預期暫存/未暫存/未追蹤計數: $output)"
    fi
  else
    fail "test_dirty_detection" "未能正確識別 dirty-wt 為 DIRTY"
  fi
}

# -----------------------------------------------------------------------------
# 測試 3: status 與 st 命令別名一致性 (test_aliases)
# -----------------------------------------------------------------------------
test_aliases() {
  local out_status out_st
  out_status="$("$SCRIPTS_DIR/wt.sh" status)"
  out_st="$("$SCRIPTS_DIR/wt.sh" st)"
  if [ "$out_status" = "$out_st" ]; then
    pass "test_aliases: status 與 st 命令輸出 100% 一致"
  else
    fail "test_aliases" "status 與 st 輸出不一致"
  fi
}

# -----------------------------------------------------------------------------
# 測試 4: 緊湊單行模式 (test_short_flag)
# -----------------------------------------------------------------------------
test_short_flag() {
  local out_short out_s
  out_short="$("$SCRIPTS_DIR/wt.sh" status --short)"
  out_s="$("$SCRIPTS_DIR/wt.sh" st -s)"
  if [ "$out_short" = "$out_s" ] && echo "$out_s" | grep -q "CLEAN.*clean-wt" && echo "$out_s" | grep -q "DIRTY.*dirty-wt"; then
    if ! echo "$out_s" | grep -q "變更項目 ("; then
      pass "test_short_flag: -s 與 --short 正確輸出緊湊單行模式且無多行展開"
    else
      fail "test_short_flag" "緊湊模式中出現了多行變更列表"
    fi
  else
    fail "test_short_flag" "-s / --short 輸出不符預期: $out_s"
  fi
}

# -----------------------------------------------------------------------------
# 測試 5: 截斷與完整變更清單模式 (test_verbose_flag)
# -----------------------------------------------------------------------------
test_verbose_flag() {
  local many_dir="$SANDBOX/many-changes"
  git -C "$BARE_DIR" worktree add -b many-branch "$many_dir" >/dev/null 2>&1

  # 建立 12 個未追蹤檔案 (超過 10 筆預設上限)
  for i in $(seq 1 12); do
    echo "file $i" > "$many_dir/file_$i.txt"
  done

  local out_default out_verbose
  out_default="$("$SCRIPTS_DIR/wt.sh" status)"
  out_verbose="$("$SCRIPTS_DIR/wt.sh" status -v)"

  local default_has_trunc=0
  local verbose_shows_all=0

  if echo "$out_default" | grep -q "尚有 2 個變更項目未顯示"; then
    default_has_trunc=1
  fi

  if echo "$out_verbose" | grep -q "file_12.txt" && ! echo "$out_verbose" | grep -q "尚有"; then
    verbose_shows_all=1
  fi

  if [ "$default_has_trunc" -eq 1 ] && [ "$verbose_shows_all" -eq 1 ]; then
    pass "test_verbose_flag: 預設限制 10 筆截斷提示，-v/--verbose 完整展開所有變更"
  else
    fail "test_verbose_flag" "verbose 或預設截斷機制未如預期運作 (default_has_trunc=$default_has_trunc, verbose_shows_all=$verbose_shows_all)"
  fi

  # 清理 many-changes 工作區
  git -C "$SANDBOX" worktree remove --force "$many_dir" >/dev/null 2>&1
}

# -----------------------------------------------------------------------------
# 測試 6: --check 旗標退出碼 (test_check_flag)
# -----------------------------------------------------------------------------
test_check_flag() {
  local exit_code_dirty=0
  "$SCRIPTS_DIR/wt.sh" st --check >/dev/null 2>&1 || exit_code_dirty=$?

  if [ "$exit_code_dirty" -eq 1 ]; then
    # 將 dirty-wt 恢復乾淨
    git -C "$DIRTY_DIR" reset --hard HEAD >/dev/null 2>&1
    git -C "$DIRTY_DIR" clean -fd >/dev/null 2>&1

    local exit_code_clean=0
    "$SCRIPTS_DIR/wt.sh" st --check >/dev/null 2>&1 || exit_code_clean=$?

    if [ "$exit_code_clean" -eq 0 ]; then
      pass "test_check_flag: 存在 dirty 工作區時 exit 1，全數 clean 時 exit 0"
    else
      fail "test_check_flag" "乾淨狀態下 --check 返回非 0 退出碼 ($exit_code_clean)"
    fi
  else
    fail "test_check_flag" "髒污狀態下 --check 未返回退出碼 1 ($exit_code_dirty)"
  fi
}

# -----------------------------------------------------------------------------
# 測試 7: 子目錄呼叫路徑解析 (test_subfolder_execution)
# -----------------------------------------------------------------------------
test_subfolder_execution() {
  local nested_dir="$CLEAN_DIR/nested/deep/subfolder"
  mkdir -p "$nested_dir"

  local output
  output="$(cd "$nested_dir" && "$SCRIPTS_DIR/wt.sh" st)"

  if echo "$output" | grep -q "工作區總覽" && echo "$output" | grep -q "clean-wt"; then
    pass "test_subfolder_execution: 於深層子目錄執行能精確定位 REPO_ROOT 並解析所有工作區"
  else
    fail "test_subfolder_execution" "深層子目錄呼叫失敗: $output"
  fi
}

# -----------------------------------------------------------------------------
# 測試 8: MISSING 工作區檢測 (test_missing_detection)
# -----------------------------------------------------------------------------
test_missing_detection() {
  local ghost_dir="$SANDBOX/ghost-wt"
  git -C "$BARE_DIR" worktree add -b ghost-branch "$ghost_dir" >/dev/null 2>&1
  # 模擬目錄被實體刪除但 worktree 未 prune
  rm -rf "$ghost_dir"

  local output
  output="$("$SCRIPTS_DIR/wt.sh" st)"

  local exit_code_missing=0
  "$SCRIPTS_DIR/wt.sh" st --check >/dev/null 2>&1 || exit_code_missing=$?

  if echo "$output" | grep -q "MISSING" && [ "$exit_code_missing" -eq 1 ]; then
    pass "test_missing_detection: 遺失目錄正確標記為 MISSING 且 --check 退出碼為 1"
  else
    fail "test_missing_detection" "未能正確檢測 MISSING 工作區 (exit_code=$exit_code_missing)"
  fi

  # 清理修剪
  git -C "$SANDBOX" worktree prune >/dev/null 2>&1
}

# 執行所有測試
test_clean_detection
test_dirty_detection
test_aliases
test_short_flag
test_verbose_flag
test_check_flag
test_subfolder_execution
test_missing_detection

echo -e "${BLUE}----------------------------------------------------------------${NC}"
echo -e "測試結果摘要: 總計 ${TOTAL_TESTS} 項 | 通過: ${GREEN}${PASSED_TESTS}${NC} | 失敗: ${RED}${FAILED_TESTS}${NC}"
echo -e "${BLUE}================================================================${NC}"

if [ "$FAILED_TESTS" -gt 0 ]; then
  exit 1
fi

exit 0
