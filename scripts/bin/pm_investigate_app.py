#!/usr/bin/env python3
"""pm_investigate_app.py — 単一アプリケーションの調査レポート生成

MCP Explorer ツール（search_entity + synthesize_answers）を直接呼び出し、
4視点 × 4データ種別の多角的分析を並列実行した後、最終レポートに統合する。

Claude Code が pm-multi-agent の MCP ツールを呼ぶのと同等の処理。

使い方:
    PYTHONPATH=scripts python3 scripts/bin/pm_investigate_app.py "アプリ名"
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("pm_investigate_app")

PERSPECTIVES = ["conservative", "aggressive", "objective", "future_oriented"]
DATA_TYPES = ["pm_data", "minutes", "slack", "box_docs"]


def investigate_app(app_name: str) -> str:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from argus.mcp_tools import search_entity, synthesize_answers

    query = f"{app_name} GPU チューニング 性能 ベンダー 協業 アーキテクチャ 連携"
    question = (
        f"pm-multi-agentを使い、{app_name} の"
        "GPU化・性能評価・ベンダー協業・アーキテクチャ連携の進捗を"
        "現状まとめ・GPU化チューニング状況・性能分析結果・ベンダー協業状況・アーキテクチャ連携"
        "の5軸で整理してレポートとしてまとめてください。"
        "マイルストーンとの連携は未整理のためレポートからは除いてください。"
    )

    # Step 1: 全16組み合わせを並列実行
    logger.info(f"Exploring {app_name}: {len(PERSPECTIVES)} perspectives × {len(DATA_TYPES)} data types")
    futures: list[tuple[str, str, object]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for p in PERSPECTIVES:
            for d in DATA_TYPES:
                fut = pool.submit(search_entity, query, p, d)
                futures.append((p, d, fut))

        results_map: dict[tuple[str, str], str] = {}
        for p, d, fut in futures:
            try:
                results_map[(p, d)] = fut.result(timeout=120)
            except Exception as e:
                logger.warning(f"Explorer error: {p}×{d}: {e}")
                results_map[(p, d)] = f"[Error] {e}"

    # Step 2: 視点ごとに統合（perspective 内の 4 data_type を synthesize）
    logger.info("Synthesizing per-perspective results...")
    combined: list[str] = []
    for p in PERSPECTIVES:
        answers = [results_map.get((p, d), "(no data)") for d in DATA_TYPES]
        combined_text = synthesize_answers(
            f"{app_name} の{p}視点での分析", answers
        )
        combined.append(combined_text)

    # Step 3: 全視点を最終統合
    logger.info("Final synthesis...")
    final = synthesize_answers(question, combined)
    return final


def main():
    if len(sys.argv) < 2:
        print("Usage: pm_investigate_app.py <app_name>", file=sys.stderr)
        sys.exit(1)

    app_name = sys.argv[1]
    result = investigate_app(app_name)
    print(result)


if __name__ == "__main__":
    main()
