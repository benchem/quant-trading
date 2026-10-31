"""pytest 共享 fixtures（离线合成数据，不触网）。

- tmp:   干净的数据目录（各测试自行用 test_board._make_store 构建合成数据）
- store: 合成板块数据集（行业 3 板块 + 概念 2 板块）上的 DataStore

背景: test_board.py 的 test_* 函数以 pytest 参数形式请求 store/tmp，
原先只有 main() harness 内手工构造，pytest 收集时缺 fixture 全部 error。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _TESTS_DIR.parent
for _p in (str(_ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_board import _make_store  # noqa: E402


@pytest.fixture
def tmp(tmp_path):
    """干净数据目录（每次测试独立）。"""
    return tmp_path


@pytest.fixture
def store(tmp_path):
    """合成板块数据集上的 DataStore（行业 + 概念，见 test_board._make_store）。

    meta["n_boards"] 只统计行业板块（board_df 仅 industry），概念数据不会
    影响行业板块相关断言。
    """
    return _make_store(tmp_path, with_concept=True)
