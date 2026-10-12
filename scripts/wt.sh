#!/usr/bin/env bash
# Nexus Seeker Git Worktree Helper Script
set -e

# 定位專案容器根目錄
# 若在 main/ 或 feature/ worktree 內，上一層為容器根目錄；若在根目錄則當前為根目錄
COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
if [ -n "$COMMON_DIR" ] && [ -d "$COMMON_DIR" ]; then
  REPO_ROOT="$(cd "$COMMON_DIR/.." 2>/dev/null && pwd)"
elif [ -f ".git" ] && grep -q "gitdir: ./.bare" .git 2>/dev/null; then
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
  status|st)
    VERBOSE=0
    SHORT=0
    CHECK=0

    while [ $# -gt 0 ]; do
      case "$1" in
        -v|--verbose)
          VERBOSE=1
          ;;
        -s|--short)
          SHORT=1
          ;;
        --check)
          CHECK=1
          ;;
        -h|--help)
          echo "用法: ./scripts/wt.sh status|st [選項]"
          echo ""
          echo "檢視所有 Git Worktrees 的 dirty 狀態、變更檔案計數及上游同步狀態"
          echo ""
          echo "選項:"
          echo "  -v, --verbose   顯示完整變更清單（不截斷）"
          echo "  -s, --short     緊湊單行模式輸出"
          echo "  --check         若有 dirty 或 missing 工作區則以退出碼 1 退出"
          echo "  -h, --help      顯示此說明訊息"
          exit 0
          ;;
        *)
          echo "未知選項: $1"
          echo "請執行 ./scripts/wt.sh status --help 檢視支援選項"
          exit 1
          ;;
      esac
      shift
    done

    # TTY 感知終端色彩設定
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
      C_RESET="\033[0m"
      C_BOLD="\033[1m"
      C_DIM="\033[2m"
      C_RED="\033[31m"
      C_GREEN="\033[32m"
      C_YELLOW="\033[33m"
      C_BLUE="\033[34m"
      C_MAGENTA="\033[35m"
      C_CYAN="\033[36m"
    else
      C_RESET=""
      C_BOLD=""
      C_DIM=""
      C_RED=""
      C_GREEN=""
      C_YELLOW=""
      C_BLUE=""
      C_MAGENTA=""
      C_CYAN=""
    fi

    TOTAL_COUNT=0
    CLEAN_COUNT=0
    DIRTY_COUNT=0
    MISSING_COUNT=0

    # 處理單一工作區狀態檢查
    inspect_worktree() {
      local wt_path="$1"
      local is_bare="$2"
      local head_hash="$3"
      local branch_name="$4"

      # 自動跳過 bare 倉儲
      if [ "$is_bare" -eq 1 ]; then
        return 0
      fi

      TOTAL_COUNT=$((TOTAL_COUNT + 1))

      # 檢查目錄是否存在
      if [ ! -d "$wt_path" ]; then
        MISSING_COUNT=$((MISSING_COUNT + 1))
        local display_branch="${branch_name:-unknown}"
        if [ "$SHORT" -eq 1 ]; then
          echo -e "${C_RED}❌ MISSING${C_RESET} ${wt_path} [${C_CYAN}${display_branch}${C_RESET}]"
        else
          echo -e "${C_BOLD}📁 ${wt_path}${C_RESET} [${C_CYAN}${display_branch}${C_RESET}]"
          echo -e "   狀態: ${C_RED}❌ MISSING (目錄不存在)${C_RESET}"
          echo ""
        fi
        return 0
      fi

      # 讀取短 hash
      local short_head
      short_head="$(git -C "$wt_path" rev-parse --short HEAD 2>/dev/null || echo "${head_hash:0:7}")"
      if [ -z "$short_head" ]; then
        short_head="unknown"
      fi

      # 讀取分支名稱
      local branch_display="$branch_name"
      if [ -z "$branch_display" ] || [ "$branch_display" = "HEAD" ]; then
        local current_ref
        current_ref="$(git -C "$wt_path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")"
        if [ "$current_ref" = "HEAD" ]; then
          branch_display="(detached HEAD)"
        else
          branch_display="$current_ref"
        fi
      fi

      # 上游同步狀態
      local upstream_info
      local upstream_short
      local upstream_counts
      upstream_counts="$(git -C "$wt_path" rev-list --left-right --count HEAD...@{upstream} 2>/dev/null || true)"
      if [ -n "$upstream_counts" ]; then
        local ahead behind
        read -r ahead behind <<< "$upstream_counts"
        if [ "$ahead" -eq 0 ] && [ "$behind" -eq 0 ]; then
          upstream_info="${C_GREEN}🟢 與上游同步${C_RESET}"
          upstream_short="與上游同步"
        elif [ "$ahead" -gt 0 ] && [ "$behind" -gt 0 ]; then
          upstream_info="${C_MAGENTA}🟣 領先 ${ahead} / 落後 ${behind}${C_RESET}"
          upstream_short="領先 ${ahead} / 落後 ${behind}"
        elif [ "$ahead" -gt 0 ]; then
          upstream_info="${C_BLUE}🔵 領先 ${ahead}${C_RESET}"
          upstream_short="領先 ${ahead}"
        else
          upstream_info="${C_YELLOW}🟠 落後 ${behind}${C_RESET}"
          upstream_short="落後 ${behind}"
        fi
      else
        upstream_info="${C_DIM}⚪ 無上游分支${C_RESET}"
        upstream_short="無上游分支"
      fi

      # 解析工作區變更檔案
      local staged_count=0
      local unstaged_count=0
      local untracked_count=0
      local conflict_count=0
      local changes_list=()

      while IFS= read -r sline || [ -n "$sline" ]; do
        [ -z "$sline" ] && continue
        local xy="${sline:0:2}"
        local filepath="${sline:3}"

        if [[ "$xy" == "DD" || "$xy" == "AU" || "$xy" == "UD" || "$xy" == "UA" || "$xy" == "DU" || "$xy" == "AA" || "$xy" == "UU" ]]; then
          conflict_count=$((conflict_count + 1))
          changes_list+=("${C_RED}[衝突]         ${filepath}${C_RESET}")
        elif [[ "$xy" == "??" ]]; then
          untracked_count=$((untracked_count + 1))
          changes_list+=("${C_DIM}[未追蹤]       ${filepath}${C_RESET}")
        elif [[ "$xy" == "!!" ]]; then
          continue
        else
          local x="${xy:0:1}"
          local y="${xy:1:1}"
          local is_s=0
          local is_u=0
          if [[ "$x" != " " && "$x" != "?" && "$x" != "!" ]]; then
            staged_count=$((staged_count + 1))
            is_s=1
          fi
          if [[ "$y" != " " && "$y" != "?" && "$y" != "!" ]]; then
            unstaged_count=$((unstaged_count + 1))
            is_u=1
          fi
          if [ "$is_s" -eq 1 ] && [ "$is_u" -eq 1 ]; then
            changes_list+=("${C_YELLOW}[暫存+未暫存]   ${filepath}${C_RESET}")
          elif [ "$is_s" -eq 1 ]; then
            changes_list+=("${C_GREEN}[已暫存]       ${filepath}${C_RESET}")
          elif [ "$is_u" -eq 1 ]; then
            changes_list+=("${C_RED}[未暫存]       ${filepath}${C_RESET}")
          fi
        fi
      done < <(git -C "$wt_path" status --porcelain=v1 2>/dev/null || true)

      local is_dirty=0
      if [ "$staged_count" -gt 0 ] || [ "$unstaged_count" -gt 0 ] || [ "$untracked_count" -gt 0 ] || [ "$conflict_count" -gt 0 ]; then
        is_dirty=1
      fi

      local detail_items=()
      [ "$staged_count" -gt 0 ] && detail_items+=("暫存: ${staged_count}")
      [ "$unstaged_count" -gt 0 ] && detail_items+=("未暫存: ${unstaged_count}")
      [ "$untracked_count" -gt 0 ] && detail_items+=("未追蹤: ${untracked_count}")
      [ "$conflict_count" -gt 0 ] && detail_items+=("衝突: ${conflict_count}")

      local details_str=""
      for ditem in "${detail_items[@]}"; do
        if [ -z "$details_str" ]; then
          details_str="$ditem"
        else
          details_str="${details_str}, ${ditem}"
        fi
      done

      if [ "$is_dirty" -eq 1 ]; then
        DIRTY_COUNT=$((DIRTY_COUNT + 1))
        if [ "$SHORT" -eq 1 ]; then
          echo -e "${C_YELLOW}⚠️  DIRTY${C_RESET}   ${wt_path} [${C_CYAN}${branch_display}${C_RESET}] (${short_head}) (${details_str}) | ${upstream_short}"
        else
          echo -e "${C_BOLD}📁 ${wt_path}${C_RESET} [${C_CYAN}${branch_display}${C_RESET}] (${C_DIM}${short_head}${C_RESET})"
          echo -e "   狀態: ${C_YELLOW}⚠️  DIRTY (${details_str})${C_RESET} | ${upstream_info}"
          echo -e "   變更項目 (${#changes_list[@]} 筆):"
          local limit=10
          if [ "$VERBOSE" -eq 1 ] || [ "${#changes_list[@]}" -le "$limit" ]; then
            for item in "${changes_list[@]}"; do
              echo -e "     $item"
            done
          else
            for ((i=0; i<limit; i++)); do
              echo -e "     ${changes_list[$i]}"
            done
            local remaining=$(( ${#changes_list[@]} - limit ))
            echo -e "     ${C_DIM}... (尚有 ${remaining} 個變更項目未顯示，使用 -v 或 --verbose 檢視完整清單)${C_RESET}"
          fi
          echo ""
        fi
      else
        CLEAN_COUNT=$((CLEAN_COUNT + 1))
        if [ "$SHORT" -eq 1 ]; then
          echo -e "${C_GREEN}✅ CLEAN${C_RESET}   ${wt_path} [${C_CYAN}${branch_display}${C_RESET}] (${short_head}) | ${upstream_short}"
        else
          echo -e "${C_BOLD}📁 ${wt_path}${C_RESET} [${C_CYAN}${branch_display}${C_RESET}] (${C_DIM}${short_head}${C_RESET})"
          echo -e "   狀態: ${C_GREEN}✅ CLEAN${C_RESET} | ${upstream_info}"
          echo ""
        fi
      fi
    }

    # 迭代解析 git worktree list --porcelain
    cur_path=""
    cur_bare=0
    cur_head=""
    cur_branch=""

    while IFS= read -r wline || [ -n "$wline" ]; do
      case "$wline" in
        worktree\ *)
          if [ -n "$cur_path" ]; then
            inspect_worktree "$cur_path" "$cur_bare" "$cur_head" "$cur_branch"
          fi
          cur_path="${wline#worktree }"
          cur_bare=0
          cur_head=""
          cur_branch=""
          ;;
        bare)
          cur_bare=1
          ;;
        HEAD\ *)
          cur_head="${wline#HEAD }"
          ;;
        branch\ refs/heads/*)
          cur_branch="${wline#branch refs/heads/}"
          ;;
        branch\ *)
          cur_branch="${wline#branch }"
          ;;
        detached)
          cur_branch="(detached HEAD)"
          ;;
        *)
          ;;
      esac
    done < <(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null || git worktree list --porcelain)

    if [ -n "$cur_path" ]; then
      inspect_worktree "$cur_path" "$cur_bare" "$cur_head" "$cur_branch"
    fi

    # 統計摘要輸出
    if [ "$SHORT" -eq 1 ]; then
      echo -e "${C_DIM}--------------------------------------------------------------------------------${C_RESET}"
      echo -e "總計: ${TOTAL_COUNT} | 乾淨: ${CLEAN_COUNT} | 髒污: ${DIRTY_COUNT} | 異常: ${MISSING_COUNT}"
    else
      echo -e "${C_BOLD}================================================================================${C_RESET}"
      echo -e "📊 工作區總覽: 總計 ${TOTAL_COUNT} | 乾淨: ${CLEAN_COUNT} | 髒污: ${DIRTY_COUNT} | 異常: ${MISSING_COUNT}"
      echo -e "${C_BOLD}================================================================================${C_RESET}"
    fi

    if [ "$CHECK" -eq 1 ]; then
      if [ "$DIRTY_COUNT" -gt 0 ] || [ "$MISSING_COUNT" -gt 0 ]; then
        exit 1
      fi
    fi
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
    echo "  ./scripts/wt.sh status|st [opts]      檢視所有 worktree 的 dirty/clean 狀態"
    echo "                                         選項: -v (--verbose), -s (--short), --check"
    echo "  ./scripts/wt.sh add <branch> [base]   新增工作區並自動複製 .env"
    echo "  ./scripts/wt.sh remove <branch>       移除工作區並清理 refs"
    ;;
esac
