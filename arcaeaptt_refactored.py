#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arcaeaptt —— Arcaea PTT (B50 + B10) 本地查分器（重构版）。

架构：
    DatabaseManager   数据库探测 / 版本识别 / 读表
    SongMatcher       名称规范化 + 多级匹配（手动映射 -> 精确 -> 子串 -> 前缀 -> 模糊）
    PTTCalculator     单曲 PTT 公式、通关奖励、定数查询、B50/B10 汇总
    Reporter          终端渲染（rich 可选 / 纯 ANSI 回退）
    CLI               argparse 参数解析与编排

设计要点：
    * 零全局可变状态：MANUAL_MAP 之类的全局字典已并入 SongMatcher 实例。
    * 红线保留：normalize() 的正则、_pkg_from_path() 的包名正则原文未改。
    * 性能：为子串/前缀匹配预建「按长度分桶」与「按首字符分桶」索引，
      并在候选集内按原始插入顺序回放，保证与原实现的选取结果（含平局）一致。
    * 健壮性：所有失败路径统一抛 ArcaeaPTTError 子类，由 CLI 兜底出口。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "0.1.0 for nuitka"

# --------------------------------------------------------------------------- #
# 可选依赖：rich（缺失时自动回退到标准库 + ANSI 转义）
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - 取决于运行环境
    from rich import box as _rich_box
    from rich.console import Console as _RichConsole
    from rich.table import Table as _RichTable

    _HAS_RICH = True
except Exception:  # ImportError 或运行环境异常都退化为纯文本
    _rich_box = None
    _RichConsole = None
    _RichTable = None
    _HAS_RICH = False


# --------------------------------------------------------------------------- #
# 常量 / 配置（原硬编码路径集中到此处，方便迁移到其它环境）
# --------------------------------------------------------------------------- #
KNOWN_PACKAGES: Tuple[Tuple[str, str], ...] = (
    ("moe.low.arc", "Arcaea 原版"),
    ("moe.inf.arc", "Arcaea Infinity"),
)

DATA_ROOT = "/data/data"
DEFAULT_DB_CANDIDATES: Tuple[str, ...] = (
    "st3",
    "./st3",
    f"{DATA_ROOT}/moe.inf.arc/files/st3",
)

MANUAL_MAP_FILENAME = "manual_map.json"
MANUAL_MAP_ENV = "ARCAEAPTT_MANUAL_MAP"

DEFAULT_MANUAL_MAP: Dict[str, str] = {
    "ifi": "〇、",
    "eightem": "8-EM",
    "gou": "Misdeed -la bonté de Dieu et l'origine du mal-",
}

SQLITE_MAGIC = b"SQLite format 3\x00"
FUZZY_THRESHOLD = 0.75

# PTT 公式分段阈值
SCORE_PM = 10_000_000
SCORE_EX = 9_800_000
SCORE_BASE = 9_500_000

# B30 + B10？—— 与原实现保持一致：Best50 之和 + Best10 之和，除以 60
BEST_MAIN = 50
BEST_RECENT = 10
PTT_DIVISOR = 60.0


# --------------------------------------------------------------------------- #
# 自定义异常
# --------------------------------------------------------------------------- #
class ArcaeaPTTError(Exception):
    """所有本工具异常的基类。"""


class DatabaseNotFoundError(ArcaeaPTTError):
    """未能定位到任何可用的 Arcaea 数据库。"""


class InvalidDatabaseError(ArcaeaPTTError):
    """数据库文件存在，但不是合法的 Arcaea scores 库。"""


class SchemaError(ArcaeaPTTError):
    """scores 表缺少必要列。"""


class SonglistError(ArcaeaPTTError):
    """定数表读取 / 解析失败。"""


class MappingError(ArcaeaPTTError):
    """手动映射表读取 / 解析失败。"""


# --------------------------------------------------------------------------- #
# ANSI 辅助
# --------------------------------------------------------------------------- #
C_TITLE = "1;36"
C_OK = "1;32"
C_WARN = "1;33"
C_ERR = "1;31"
C_DIM = "2"

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, code: str) -> str:
    """按 TTY 能力包裹 ANSI 颜色码。"""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


# --------------------------------------------------------------------------- #
# 纯函数：规范化 / 包名解析（红线逻辑，原样保留）
# --------------------------------------------------------------------------- #
def normalize(s: Any) -> str:
    """规范化歌曲名：转小写并剔除 [a-z0-9] 之外的一切字符。

    注意：正则表达式 `r'[^a-z0-9]'` 属于不可改动逻辑。
    """
    return re.sub(r'[^a-z0-9]', '', str(s).lower())


def _pkg_from_path(path: Optional[str]) -> Optional[str]:
    """从路径中解析 Arcaea 包名。

    注意：`/data/data/([^/]+)/` 正则属于不可改动逻辑。
    """
    if not path:
        return None
    m = re.search(r'/data/data/([^/]+)/', str(path))
    if m:
        return m.group(1)
    for pkg, _ in KNOWN_PACKAGES:
        if pkg in str(path):
            return pkg
    return None


def is_valid_arcaea_db(path: str) -> bool:
    """校验给定文件是否为含 scores 表的 SQLite 数据库。"""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            if not f.read(16).startswith(SQLITE_MAGIC):
                return False
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scores'"
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def find_db(paths: Sequence[str]) -> Optional[str]:
    """按「用户给定路径 -> 默认候选路径」顺序寻找有效数据库。"""
    for p in list(paths) + list(DEFAULT_DB_CANDIDATES):
        if is_valid_arcaea_db(p):
            return p
    return None


def calculate_single_ptt(
    chart_constant: float, score: int, clear_bonus: float = 0.0
) -> float:
    """单曲 PTT（公式与原实现逐字一致）。"""
    if score >= SCORE_PM:
        ptt = chart_constant + 2.0 + clear_bonus
    elif score >= SCORE_EX:
        ptt = chart_constant + 1.0 + (score - SCORE_EX) / 200_000.0 + clear_bonus
    else:
        ptt = chart_constant + (score - SCORE_BASE) / 300_000.0 + clear_bonus
        return max(0.0, ptt)
    return ptt


def clear_bonus_from_ct(ct: Any) -> float:
    """通关类型加成。

    ct: 0=TL, 1=TC, 2=FR, 3=PM, 4=Easy, 5=Hard
    规则：PM/FR/TC/EasyClear/HardClear +0.200；TL +0.000
    """
    if ct is None:
        return 0.0
    try:
        value = int(ct)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if value == 0 else 0.2


def diff_label(difficulty: Any) -> str:
    """难度键 -> 人类可读难度名。"""
    d = str(difficulty)
    if d == "3":
        return "Beyond"
    if d == "4":
        return "Eternal"
    return {"0": "Past", "1": "Present", "2": "Future"}.get(d, d)


def ct_label(ct: Any) -> str:
    """通关类型 -> 人类可读名称。"""
    try:
        value = int(ct)
    except (TypeError, ValueError):
        return str(ct)
    return {
        0: "Track Lost",
        1: "Track Complete",
        2: "Full Recall",
        3: "Pure Memory",
        4: "Easy Clear",
        5: "Hard Clear",
    }.get(value, f"ct{value}")


def get_constant(
    entry: Optional[Dict[str, Any]], difficulty: Any
) -> Tuple[Optional[float], bool, Optional[str]]:
    """返回 (定数, 是否回退, 实际使用的难度键)。只做精确匹配，不回退 BYD<->ETR。"""
    if entry is None:
        return None, False, None
    key = str(difficulty)
    if key in entry:
        value = entry[key]
        if isinstance(value, (int, float)):
            return float(value), False, key
    return None, False, None


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScoreRow:
    """scores 表去重后的最高分记录。"""

    song_id: Any
    difficulty: Any
    score: int
    ct: int


@dataclass
class PTTRecord:
    """一条已算出 PTT 的成绩。"""

    db_song_id: Any
    json_key: str
    difficulty: Any
    diff_name: str
    used_key: Optional[str]
    is_fallback: bool
    score: int
    constant: float
    ptt: float
    ct: int
    bonus: float


@dataclass
class RunStats:
    """统计信息，供 debug 输出。"""

    total_songs: int = 0
    valid_keys: int = 0
    db_rows: int = 0
    cleartypes: int = 0
    matched: int = 0
    ct_zero: int = 0
    ct_recovered: int = 0
    fallback: int = 0
    unmatched_song: List[Tuple[Any, Any, int]] = field(default_factory=list)
    unmatched_diff: List[Tuple[Any, Any, int, str, Dict[str, Any]]] = field(
        default_factory=list
    )


# --------------------------------------------------------------------------- #
# DatabaseManager
# --------------------------------------------------------------------------- #
class DatabaseManager:
    """Arcaea SQLite 数据库的探测、版本识别与读取。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None

    # -- 探测 ---------------------------------------------------------------
    @property
    def path(self) -> str:
        return self._path

    @staticmethod
    def find(paths: Sequence[str]) -> str:
        """定位数据库，失败抛 DatabaseNotFoundError。"""
        found = find_db(paths)
        if not found:
            raise DatabaseNotFoundError(
                "未找到有效的 Arcaea 数据库（可手动传入路径，例如 /data/data/moe.inf.arc/files/st3）"
            )
        return found

    @classmethod
    def detect_version(cls, db_path: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """识别原版 / Infinity 端，返回 (显示名, 包名)。"""
        try:
            cwd: Optional[str] = os.getcwd()
        except OSError:
            cwd = None

        for src in (db_path, cwd):
            pkg = _pkg_from_path(src)
            if pkg:
                for kp, kname in KNOWN_PACKAGES:
                    if pkg == kp:
                        return kname, kp

        try:
            result = subprocess.run(
                ["pm", "list", "packages"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = (result.stdout or "") + (result.stderr or "")
            for pkg, name in KNOWN_PACKAGES:
                if f"package:{pkg}" in out:
                    return name, pkg
        except Exception:
            pass

        for pkg, name in KNOWN_PACKAGES:
            if os.path.isdir(f"{DATA_ROOT}/{pkg}"):
                return name, pkg

        return None, None

    # -- 连接管理 -----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if not is_valid_arcaea_db(self._path):
            raise InvalidDatabaseError(f"不是有效的 Arcaea 数据库：{self._path}")
        try:
            self._conn = sqlite3.connect(self._path)
        except sqlite3.Error as exc:
            raise InvalidDatabaseError(f"数据库打开失败：{self._path}（{exc}）") from exc
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "DatabaseManager":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- 读取 ---------------------------------------------------------------
    def read_cleartypes(self) -> Dict[Tuple[Any, Any], Any]:
        """读取 cleartypes 表，用于 ct=0 时的回退。缺失表时返回空字典。"""
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT songId, songDifficulty, clearType FROM cleartypes")
            return {(sid, diff): ctype for sid, diff, ctype in cur.fetchall()}
        except sqlite3.Error:
            return {}

    def read_best_scores(self) -> List[ScoreRow]:
        """取每个谱面的最高分记录（连同 ct）。"""
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA table_info(scores)")
            columns = [col[1] for col in cur.fetchall()]
        except sqlite3.Error as exc:
            raise InvalidDatabaseError(f"读取 scores 表结构失败：{exc}") from exc

        missing = [c for c in ("songId", "songDifficulty", "score") if c not in columns]
        if missing:
            raise SchemaError(
                f"scores 表缺少必要列 {missing}。实际列: {columns}"
            )
        has_ct = "ct" in columns

        if has_ct:
            query = """
                SELECT songId, songDifficulty, score, ct
                FROM (
                    SELECT songId, songDifficulty, score, ct,
                           ROW_NUMBER() OVER (
                               PARTITION BY songId, songDifficulty
                               ORDER BY score DESC
                           ) AS rn
                    FROM scores
                )
                WHERE rn = 1
            """
        else:
            query = """
                SELECT songId, songDifficulty, score, 0 AS ct
                FROM (
                    SELECT songId, songDifficulty, score,
                           ROW_NUMBER() OVER (
                               PARTITION BY songId, songDifficulty
                               ORDER BY score DESC
                           ) AS rn
                    FROM scores
                )
                WHERE rn = 1
            """

        try:
            cur.execute(query)
            return [ScoreRow(sid, diff, score, ct) for sid, diff, score, ct in cur.fetchall()]
        except sqlite3.Error as exc:
            raise InvalidDatabaseError(f"查询 scores 失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# 手动映射加载
# --------------------------------------------------------------------------- #
def _coerce_map(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def load_manual_map(path: str) -> Dict[str, str]:
    """从 JSON 文件读取手动映射（songId -> 定数表键名）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise MappingError(f"解析 manual_map 失败 ({path}): {exc}") from exc
    except OSError as exc:
        raise MappingError(f"读取 manual_map 失败 ({path}): {exc}") from exc
    return _coerce_map(data)


def load_manual_map_from_env(var: str = MANUAL_MAP_ENV) -> Dict[str, str]:
    """从环境变量读取手动映射。"""
    raw = os.environ.get(var)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MappingError(f"解析环境变量 {var} 失败: {exc}") from exc
    return _coerce_map(data)


# --------------------------------------------------------------------------- #
# SongMatcher
# --------------------------------------------------------------------------- #
class SongMatcher:
    """歌曲名多级匹配器。

    匹配顺序（不可删减）：手动映射 -> 精确 -> 子串 -> 前缀 -> difflib 模糊。
    索引：_by_len[长度] / _by_first[首字符]，并保留插入序号以复现原实现的平局行为。
    """

    def __init__(
        self,
        songlist: Dict[str, Dict[str, Any]],
        manual_map: Optional[Dict[str, str]] = None,
        threshold: float = FUZZY_THRESHOLD,
    ) -> None:
        self.threshold = threshold
        self.manual_map: Dict[str, str] = dict(DEFAULT_MANUAL_MAP)
        if manual_map:
            self.manual_map.update(_coerce_map(manual_map))

        self._index: Dict[str, str] = {}
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._raw_index: Dict[str, Dict[str, Any]] = {}
        self._order: Dict[str, int] = {}
        # 前缀匹配索引：按首字符分桶（startswith 必然共享首字符）
        self._by_first: Dict[str, List[str]] = {}
        # 子串匹配索引：4-gram -> 含该 gram 的键（用于「norm 是 jn 子串」方向）
        self._gram4: Dict[str, List[str]] = {}
        self._build_index(songlist)

    # -- 构建 ---------------------------------------------------------------
    def _build_index(self, songlist: Dict[str, Dict[str, Any]]) -> None:
        if not isinstance(songlist, dict):
            raise SonglistError("定数表根节点必须是 JSON 对象")

        counter = 0
        for key, entry in songlist.items():
            self._raw_index[key] = entry
            norm = normalize(key)
            if not norm:
                continue
            self._index[norm] = key  # 与原实现一致：重复键后者覆盖
            self._entries[norm] = entry
            if norm not in self._order:  # 但保留首次出现的位置
                self._order[norm] = counter
                counter += 1
                self._by_first.setdefault(norm[0], []).append(norm)
                for gram in {norm[i : i + 4] for i in range(len(norm) - 3)}:
                    self._gram4.setdefault(gram, []).append(norm)

    # -- 对外 ---------------------------------------------------------------
    def find(self, song_id: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """返回 (定数表键名, 定数信息)；找不到返回 (None, None)。"""
        sid_text = str(song_id)

        # 1) 手动映射
        manual = self.manual_map.get(sid_text)
        if manual is None and not isinstance(song_id, str):
            manual = self.manual_map.get(song_id)  # type: ignore[arg-type]
        if manual is not None:
            if manual in self._raw_index:
                return manual, self._raw_index[manual]
            n_target = normalize(manual)
            if n_target in self._entries:
                return self._index[n_target], self._entries[n_target]

        norm = normalize(sid_text)
        if not norm:
            if sid_text in self._raw_index:
                return sid_text, self._raw_index[sid_text]
            return None, None

        # 2) 精确
        if norm in self._entries:
            return self._index[norm], self._entries[norm]

        # 3) 子串
        hit = self._match_substring(norm)
        if hit:
            return hit

        # 4) 前缀
        hit = self._match_prefix(norm)
        if hit:
            return hit

        # 5) difflib 模糊
        return self._match_fuzzy(norm)

    # -- 各级匹配 -----------------------------------------------------------
    def _match_substring(self, norm: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """双向子串匹配，最长者胜；平局取插入顺序靠前者（与原实现一致）。"""
        candidates: List[str] = []
        seen: set = set()

        # 分支 A：4 <= len(jn) <= len(norm) 且 jn 是 norm 的子串
        # 直接枚举 norm 的全部长度 >= 4 的子串，命中即候选（O(L^2) 次查表）
        length = len(norm)
        for i in range(length):
            for j in range(i + 4, length + 1):
                sub = norm[i:j]
                if sub in self._entries and sub not in seen:
                    seen.add(sub)
                    candidates.append(sub)

        # 分支 B：len(norm) >= 4 且 norm 是 jn 的子串
        # 用 norm 前 4 个字符做 4-gram 倒排索引过滤，再逐条校验
        if length >= 4:
            for jn in self._gram4.get(norm[:4], ()):
                if jn in seen:
                    continue
                if norm in jn:
                    seen.add(jn)
                    candidates.append(jn)

        if not candidates:
            return None

        candidates.sort(key=self._order.__getitem__)

        best: Optional[Tuple[str, Dict[str, Any]]] = None
        best_len = 0
        for jn in candidates:
            # 保持原实现的 if/elif 语义：A 命中则不再走 B
            if len(jn) >= 4 and jn in norm:
                if len(jn) > best_len:
                    best = (self._index[jn], self._entries[jn])
                    best_len = len(jn)
            elif len(norm) >= 4 and norm in jn:
                if len(norm) > best_len:
                    best = (self._index[jn], self._entries[jn])
                    best_len = len(norm)
        return best

    def _match_prefix(self, norm: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """前缀匹配，长度差最小者胜。候选必与 norm 同首字符。"""
        bucket = self._by_first.get(norm[0], ())
        if not bucket:
            return None
        best: Optional[Tuple[str, Dict[str, Any]]] = None
        best_diff = 999
        for jn in sorted(bucket, key=self._order.__getitem__):
            if jn.startswith(norm) or norm.startswith(jn):
                diff = abs(len(jn) - len(norm))
                if diff < best_diff:
                    best_diff = diff
                    best = (self._index[jn], self._entries[jn])
        return best

    def _match_fuzzy(self, norm: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        matches = difflib.get_close_matches(
            norm, list(self._entries.keys()), n=1, cutoff=self.threshold
        )
        if not matches:
            return None, None
        m = matches[0]
        return self._index[m], self._entries[m]

    # -- 统计 ---------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------------------- #
# PTTCalculator
# --------------------------------------------------------------------------- #
class PTTCalculator:
    """分数计算与 Best 汇总。"""

    single_ptt = staticmethod(calculate_single_ptt)
    clear_bonus = staticmethod(clear_bonus_from_ct)
    constant_of = staticmethod(get_constant)
    diff_label = staticmethod(diff_label)
    ct_label = staticmethod(ct_label)

    def __init__(self, matcher: SongMatcher) -> None:
        self._matcher = matcher

    def build_record(self, row: ScoreRow, effective_ct: Any) -> Optional[PTTRecord]:
        """把数据库行转成 PTTRecord；歌曲未匹配返回 None。"""
        json_key, entry = self._matcher.find(row.song_id)
        if entry is None:
            return None
        constant, is_fallback, used_key = get_constant(entry, row.difficulty)
        if constant is None:
            return None
        bonus = clear_bonus_from_ct(effective_ct)
        ptt = calculate_single_ptt(constant, row.score, bonus)
        return PTTRecord(
            db_song_id=row.song_id,
            json_key=json_key,
            difficulty=row.difficulty,
            diff_name=diff_label(row.difficulty),
            used_key=used_key,
            is_fallback=is_fallback,
            score=row.score,
            constant=constant,
            ptt=ptt,
            ct=int(effective_ct) if effective_ct is not None else 0,
            bonus=bonus,
        )

    @staticmethod
    def summarize(
        records: Sequence[PTTRecord],
    ) -> Tuple[List[PTTRecord], List[PTTRecord], float]:
        """返回 (best50, best10, 总 PTT)。"""
        ordered = sorted(records, key=lambda x: x.ptt, reverse=True)
        best_main = ordered[:BEST_MAIN]
        best_recent = ordered[:BEST_RECENT]
        total = (sum(r.ptt for r in best_main) + sum(r.ptt for r in best_recent)) / PTT_DIVISOR
        return best_main, best_recent, total


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #
class Reporter:
    """终端渲染：优先 rich，缺失或缺色时回退纯文本。"""

    def __init__(self, debug: bool = False, use_rich: bool = True) -> None:
        self.debug = debug
        self._rich = bool(use_rich and _HAS_RICH)
        self._console = _RichConsole() if self._rich else None

    # -- 基础输出 -----------------------------------------------------------
    def info(self, msg: str) -> None:
        print(msg)

    def title(self, msg: str) -> None:
        print(_c(msg, C_TITLE))

    def warn(self, msg: str) -> None:
        print(_c(msg, C_WARN))

    def error(self, msg: str) -> None:
        print(_c(msg, C_ERR), file=sys.stderr)

    # -- 表格 ---------------------------------------------------------------
    def top_table(self, records: Sequence[PTTRecord]) -> None:
        if self._rich:
            table = _RichTable(box=_rich_box.SIMPLE_HEAD, header_style="bold cyan")
            table.add_column("排名", justify="right")
            table.add_column("PTT", justify="right")
            table.add_column("定数", justify="right")
            table.add_column("分数", justify="right")
            table.add_column("通关")
            table.add_column("歌曲")
            table.add_column("难度")
            for i, r in enumerate(records, 1):
                table.add_row(
                    str(i),
                    f"{r.ptt:.4f}",
                    f"{r.constant:.1f}",
                    str(r.score),
                    ct_label(r.ct),
                    r.json_key,
                    r.diff_name,
                )
            self._console.print(table)
            return

        header = (
            f"{'排名':<4} {'PTT':<9} {'定数':<6} {'分数':<10} "
            f"{'通关':<16} {'歌曲':<40} {'难度'}"
        )
        print(_c(header, C_TITLE))
        for i, r in enumerate(records, 1):
            print(
                f"{i:<4} {r.ptt:<9.4f} {r.constant:<6.1f} {r.score:<10} "
                f"{ct_label(r.ct):<16} {r.json_key:<40} {r.diff_name}"
            )

    def match_detail(self, records: Sequence[PTTRecord]) -> None:
        print("\n匹配详情：")
        for r in records:
            tag = " [回退]" if r.is_fallback else ""
            print(
                f"  {str(r.db_song_id):<25} -> {r.json_key:<40} | "
                f"难度 {r.difficulty} ({r.diff_name}) | 用键 \"{r.used_key}\" | "
                f"定数 {r.constant} | 分数 {r.score} | "
                f"{ct_label(r.ct)}(bonus={r.bonus}){tag}"
            )

    def unmatched_diff(self, rows: Sequence[Tuple[Any, Any, int, str, Dict[str, Any]]]) -> None:
        print(f"\n【缺少难度定数】{len(rows)} 条：")
        for sid, diff, score, jkey, entry in rows:
            avail = sorted(entry.keys())
            print(
                f"  songId={str(sid):<22} -> {jkey:<40} | "
                f"难度 {diff} ({diff_label(diff)}) 缺失 | "
                f"JSON 可用键: {avail} | 分数 {score}"
            )

    def unmatched_song(self, rows: Sequence[Tuple[Any, Any, int]]) -> None:
        print(f"\n【歌曲未匹配】{len(rows)} 条：")
        for sid, diff, score in rows:
            print(f"  songId={sid}, difficulty={diff}, score={score}")


# --------------------------------------------------------------------------- #
# 手动映射装配
# --------------------------------------------------------------------------- #
def resolve_manual_map(
    cli_map: Optional[str], songlist_path: str, reporter: Reporter
) -> Dict[str, str]:
    """优先级：--map > 定数表同目录 > 当前目录 > 环境变量。"""
    candidates: List[str] = []
    if cli_map:
        candidates.append(cli_map)
    songlist_dir = os.path.dirname(os.path.abspath(songlist_path))
    candidates.append(os.path.join(songlist_dir, MANUAL_MAP_FILENAME))
    candidates.append(os.path.join(os.getcwd(), MANUAL_MAP_FILENAME))

    for cand in candidates:
        try:
            loaded = load_manual_map(cand)
        except MappingError as exc:
            reporter.error(str(exc))
            continue
        if loaded:
            reporter.info(f"已从 {cand} 加载 {len(loaded)} 条手动映射")
            return loaded

    try:
        env_map = load_manual_map_from_env()
    except MappingError as exc:
        reporter.error(str(exc))
        env_map = {}
    if env_map:
        reporter.info(f"已从环境变量 {MANUAL_MAP_ENV} 加载 {len(env_map)} 条手动映射")
    return env_map


def load_songlist(path: str) -> Dict[str, Dict[str, Any]]:
    """读取定数表 JSON。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise SonglistError(f"定数表不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SonglistError(f"定数表解析失败 ({path}): {exc}") from exc
    except OSError as exc:
        raise SonglistError(f"定数表读取失败 ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise SonglistError("定数表根节点必须是 JSON 对象")
    return data


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class CLI:
    """命令行入口。"""

    def __init__(self, argv: Optional[Sequence[str]] = None) -> None:
        self._argv = list(argv) if argv is not None else None
        self._parser = self._build_parser()

    @staticmethod
    def _build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="计算 Arcaea PTT (B50 + B10)")
        parser.add_argument("paths", nargs="*", help="数据库路径")
        parser.add_argument("--songlist", "-s", required=True, help="谱面定数 JSON")
        parser.add_argument("--debug", "-d", action="store_true", help="打印匹配详情")
        parser.add_argument(
            "--version",
            "-v",
            action="version",
            version=f"arcaeaptt {VERSION}",
            help="显示版本号并退出",
        )
        parser.add_argument(
            "--map", "-m", help="手动映射 JSON 文件（songId -> 定数表键名）"
        )
        parser.add_argument(
            "--threshold",
            "-t",
            type=float,
            default=FUZZY_THRESHOLD,
            help="difflib 模糊匹配阈值（默认 0.75）",
        )
        parser.add_argument(
            "--no-rich", action="store_true", help="禁用 rich，强制纯文本/ANSI 输出"
        )
        return parser

    def run(self) -> int:
        args = self._parser.parse_args(self._argv)
        reporter = Reporter(debug=args.debug, use_rich=not args.no_rich)
        try:
            return self._execute(args, reporter)
        except ArcaeaPTTError as exc:
            reporter.error(f"错误：{exc}")
            return 1
        except KeyboardInterrupt:
            reporter.error("已中断")
            return 130

    def _execute(self, args: argparse.Namespace, reporter: Reporter) -> int:
        db_path = DatabaseManager.find(args.paths)
        version_name, pkg_name = DatabaseManager.detect_version(db_path)
        songlist = load_songlist(args.songlist)
        manual_map = resolve_manual_map(args.map, args.songlist, reporter)

        matcher = SongMatcher(songlist, manual_map, threshold=args.threshold)
        calculator = PTTCalculator(matcher)

        reporter.title(f"arcaeaptt v{VERSION}")
        reporter.info(f"使用数据库: {db_path}")
        if version_name:
            reporter.info(f"检测到版本: {version_name} ({pkg_name})")
        else:
            reporter.info("检测到版本: 未知（未识别到已知 Arcaea 包名）")
        reporter.info(f"定数表歌曲数: {len(songlist)}（有效规范化键 {matcher.size}）")

        stats = RunStats(total_songs=len(songlist), valid_keys=matcher.size)

        with DatabaseManager(db_path) as db:
            clear_map = db.read_cleartypes()
            if clear_map:
                reporter.info(f"cleartypes 记录数: {len(clear_map)}")
            else:
                reporter.warn("cleartypes 表不可用（已忽略 ct 回退）")

            rows = db.read_best_scores()
            stats.db_rows = len(rows)
            stats.cleartypes = len(clear_map)
            reporter.info(f"数据库记录数（去重后）: {len(rows)}")

            results: List[PTTRecord] = []
            for row in rows:
                if row.score is None:
                    continue

                effective_ct = row.ct
                if effective_ct == 0:
                    stats.ct_zero += 1
                    fallback_ct = clear_map.get((row.song_id, row.difficulty), 0)
                    if fallback_ct != 0:
                        effective_ct = fallback_ct
                        stats.ct_recovered += 1

                json_key, entry = matcher.find(row.song_id)
                if entry is None:
                    stats.unmatched_song.append((row.song_id, row.difficulty, row.score))
                    continue

                constant, is_fallback, used_key = get_constant(entry, row.difficulty)
                if constant is None:
                    stats.unmatched_diff.append(
                        (row.song_id, row.difficulty, row.score, json_key, entry)
                    )
                    continue

                bonus = clear_bonus_from_ct(effective_ct)
                results.append(
                    PTTRecord(
                        db_song_id=row.song_id,
                        json_key=json_key,
                        difficulty=row.difficulty,
                        diff_name=diff_label(row.difficulty),
                        used_key=used_key,
                        is_fallback=is_fallback,
                        score=row.score,
                        constant=constant,
                        ptt=calculate_single_ptt(constant, row.score, bonus),
                        ct=int(effective_ct) if effective_ct is not None else 0,
                        bonus=bonus,
                    )
                )
                if is_fallback:
                    stats.fallback += 1

        stats.matched = len(results)
        reporter.info(f"\n成功匹配定数的成绩数: {stats.matched}")
        reporter.info(f"未匹配歌曲: {len(stats.unmatched_song)} 条")
        reporter.info(f"缺少难度定数: {len(stats.unmatched_diff)} 条")
        reporter.info(f"ct=0（TL）条数: {stats.ct_zero}")
        reporter.info(f"其中通过 cleartypes 找回非 TL 的条数: {stats.ct_recovered}")
        reporter.info(f"回退匹配（BYD/ETR 混用）: {stats.fallback} 条")

        if not results:
            reporter.warn("\n⚠️ 没有任何成绩匹配到定数。")
            return 0

        best_main, best_recent, total_ptt = PTTCalculator.summarize(results)

        if not args.debug:
            # 机读输出，保持原格式
            print(f"ptt:{total_ptt:.4f}")
            return 0

        reporter.match_detail(results)
        if stats.unmatched_diff:
            reporter.unmatched_diff(stats.unmatched_diff)
        if stats.unmatched_song:
            reporter.unmatched_song(stats.unmatched_song)

        print("\n" + "=" * 60)
        print(f"总 PTT: {total_ptt:.4f}")
        print(f"Best 50 数量: {len(best_main)}")
        print(f"Best 10 数量: {len(best_recent)}")
        print("=" * 60)

        print("\n前 10 成绩：")
        reporter.top_table(best_recent)

        print("\nBest 50 列表：")
        if reporter._rich:  # 有 rich 时复用表格渲染
            reporter.top_table(best_main)
        else:
            for i, r in enumerate(best_main, 1):
                print(
                    f"{i:<3} PTT: {r.ptt:.4f} | 定数: {r.constant:.1f} | "
                    f"分数: {r.score} | 通关: {ct_label(r.ct)} | "
                    f"歌曲: {r.json_key} | 难度: {r.diff_name}"
                )
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return CLI(argv).run()


if __name__ == "__main__":
    sys.exit(main())