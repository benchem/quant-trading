"""全局默认配置。"""
from __future__ import annotations

from pathlib import Path

# 数据落盘根目录（相对运行时工作目录，可用 --data-dir 覆盖）
DEFAULT_DATA_DIR = Path("data")

# 复权方式：hfq=后复权（建模推荐，历史值稳定）/ qfq=前复权 / none=不复权
DEFAULT_ADJUST = "hfq"
ADJUST_CHOICES = ("hfq", "qfq", "none")
ADJUST_LABEL = {"hfq": "后复权", "qfq": "前复权", "none": "不复权"}

# 默认下载起始日期（上交所开市日）
DEFAULT_START = "19901219"

# 并发与限速：对数据源请求的总速率上限（次/秒）
DEFAULT_WORKERS = 4
DEFAULT_RATE = 6.0

RETRIES = 3

# 主要宽基指数（指数行情，代码为 Wind 风格）
MAJOR_INDICES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
}
