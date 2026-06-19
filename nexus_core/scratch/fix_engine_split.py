"""
fix_engine_split.py

修正：gamma_squeeze_engine.py 誤包含 IntradayScanPipeline。
執行後：
  - gamma_squeeze_engine.py  只保留 NexusGammaSqueezeEngine
  - intraday_pipeline.py     接回 IntradayScanPipeline 及其 helper 函式
"""

import ast
import re
import sys
from pathlib import Path

NEXUS_CORE = Path(__file__).parent.parent
GAMMA_ENG = NEXUS_CORE / "market_analysis" / "gamma_squeeze_engine.py"
PIPELINE = NEXUS_CORE / "market_analysis" / "intraday_pipeline.py"


def verify_syntax(path: Path, source: str) -> None:
    try:
        ast.parse(source)
        print(f"  ✅ Syntax OK: {path.name}")
    except SyntaxError as e:
        print(f"  ❌ Syntax Error in {path.name}: {e}")
        sys.exit(1)


def main() -> None:
    # ── 1. 讀取 gamma_squeeze_engine.py ─────────────────────────────────
    eng_src = GAMMA_ENG.read_text(encoding="utf-8")
    eng_lines = eng_src.splitlines(keepends=True)

    # 找 IntradayScanPipeline 的起始行（與前兩個空行）
    pipe_start = None
    for i, ln in enumerate(eng_lines):
        if re.match(r"^class IntradayScanPipeline", ln):
            # 往前找空行邊界
            pipe_start = i
            while pipe_start > 0 and eng_lines[pipe_start - 1].strip() == "":
                pipe_start -= 1
            break

    if pipe_start is None:
        print(
            "  ℹ️  IntradayScanPipeline not found in gamma_squeeze_engine.py — nothing to do."
        )
        return

    print(
        f"  Found IntradayScanPipeline at line {pipe_start + 1} of gamma_squeeze_engine.py"
    )

    # 分割：engine-only vs pipeline block
    engine_only = "".join(eng_lines[:pipe_start]).rstrip() + "\n"
    pipeline_block = "".join(eng_lines[pipe_start:])

    verify_syntax(GAMMA_ENG, engine_only)
    GAMMA_ENG.write_text(engine_only, encoding="utf-8")
    print(f"  Updated: {GAMMA_ENG} ({len(engine_only.splitlines())} lines)")

    # ── 2. 把 IntradayScanPipeline 追加回 intraday_pipeline.py ──────────
    pipe_src = PIPELINE.read_text(encoding="utf-8")

    # 補上 IntradayScanPipeline 需要的 imports（如果 pipeline.py 頂部還沒有）
    extra_imports = []
    needed = {
        "WatchlistEvaluation": "from models.schemas import WatchlistEvaluation",
        "WatchlistRiskController": "from risk_engine.nro import WatchlistRiskController",
    }
    for symbol, stmt in needed.items():
        if symbol not in pipe_src and stmt not in pipe_src:
            extra_imports.append(stmt)

    # 在 logger 行前插入缺少的 imports
    insert_pos = pipe_src.find("logger = logging.getLogger(__name__)")
    if extra_imports and insert_pos != -1:
        extra_str = "\n".join(extra_imports) + "\n\n"
        pipe_src = pipe_src[:insert_pos] + extra_str + pipe_src[insert_pos:]

    new_pipeline = pipe_src.rstrip() + "\n\n\n" + pipeline_block
    verify_syntax(PIPELINE, new_pipeline)
    PIPELINE.write_text(new_pipeline, encoding="utf-8")
    print(f"  Updated: {PIPELINE} ({len(new_pipeline.splitlines())} lines)")
    print("\n✅ Fix complete.")


if __name__ == "__main__":
    main()
