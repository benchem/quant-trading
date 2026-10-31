"""实测板块抓取接口（需要网络）。运行: .venv/bin/python tests/test_board_live.py

验证：
- 东财行业板块列表（m:90 t:2，约 496 个）
- 单板块成分股分页
- 全量成分表：代码规范化、每板块至少 1 只、与股票列表的覆盖率
"""
from __future__ import annotations

import sys
import traceback

import pytest

sys.path.insert(0, ".")
from downloader import source  # noqa: E402

PASSED = 0
FAILED = 0
SKIPPED = 0


class Skip(Exception):
    """东财暂不可用（限流/停服/兜底依赖缺失）时跳过——独立运行 harness 用。"""


def _running_under_pytest() -> bool:
    return any(m == "pytest" or m == "_pytest" or m.startswith("_pytest.")
               for m in sys.modules)


def _skip(reason: str) -> None:
    """数据源/网络/依赖不可用 -> 跳过（不是失败）。

    pytest 下走 pytest.skip（原自定义 Skip 异常会被 pytest 记为 FAILED）；
    独立运行时仍抛 Skip 由 main() harness 统计。
    """
    if _running_under_pytest():
        pytest.skip(reason)
    raise Skip(reason)


def check(name: str, fn) -> bool:
    global PASSED, FAILED, SKIPPED
    print(f"\n===== {name} =====", flush=True)
    try:
        fn()
        print("PASS")
        PASSED += 1
        return True
    except Skip as s:
        print(f"SKIP（{s}）")
        SKIPPED += 1
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED += 1
        return False


def test_board_list():
    try:
        boards = source._em_board_list()
    except Exception as e:  # noqa: BLE001
        _skip(f"东财板块列表不可用: {str(e)[:80]}")
    assert len(boards) >= 100, f"板块数过少: {len(boards)}"
    assert set(boards.columns) >= {"board_code", "board_name"}
    assert boards["board_code"].str.match(r"^BK\d+$").all()
    print(f"行业板块: {len(boards)} 个，示例: "
          f"{boards[['board_code', 'board_name']].head(3).values.tolist()}")


def test_board_members_one():
    try:
        boards = source._em_board_list()
    except Exception as e:  # noqa: BLE001
        _skip(f"东财板块列表不可用: {str(e)[:80]}")
    # 取成分股最多的前 3 个板块逐个验证（覆盖多页场景：pz=200）
    for _, b in boards.head(3).iterrows():
        ms = source._em_board_members(b["board_code"])
        assert len(ms) > 0, f"{b['board_code']} 成分为空"
        assert all(len(str(c)) == 6 and str(c).isdigit() for c in ms[:5])
        print(f"  {b['board_code']} {b['board_name']}: {len(ms)} 只")


def test_full_members():
    try:
        df, failed = source.fetch_board_members()
    except Exception as e:  # noqa: BLE001
        # 东财不可用且 baostock 兜底依赖缺失/网络不通 -> 跳过（环境性，非代码缺陷）
        _skip(f"板块成分数据源不可用: {str(e)[:80]}")
    assert len(df) > 3000, f"成分数过少: {len(df)}"
    assert len(df["code"].unique()) == len(df), "存在重复股票代码（行业分类应为划分）"
    n_boards = df["board_code"].nunique()
    print(f"全量成分: {len(df)} 只股票 / {n_boards} 个板块 | 失败板块: {len(failed)}")
    if failed:
        print(f"  失败: {failed[:10]}")
    # 覆盖率：成分表代码应几乎都在股票列表中（列表含科创板等全量）
    try:
        import pandas as pd
        from pathlib import Path

        sl = pd.read_parquet(Path("data/stock_list.parquet"), engine="pyarrow")
        in_list = df["code"].isin(set(sl["code"])).mean()
        print(f"与 stock_list 覆盖率: {in_list:.1%}")
        assert in_list > 0.95, f"覆盖率过低: {in_list:.1%}"
    except FileNotFoundError:
        print("  (无本地 stock_list，跳过覆盖率检查)")


def main() -> int:
    check("行业板块列表（东财）", test_board_list)
    check("单板块成分（东财，前3板块）", test_board_members_one)
    check("全量板块成分表（东财，baostock 兜底）", test_full_members)
    print(f"\n===== 汇总: {PASSED} 通过 / {FAILED} 失败 / {SKIPPED} 跳过 =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
