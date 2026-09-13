#!/usr/bin/env python3
VERSION = "0.0.2"
import sqlite3
import sys
import os
import json
import re
import difflib
import argparse

# ========== 版本检测 ==========
KNOWN_PACKAGES = [
    ('moe.low.arc', 'Arcaea 原版'),
    ('moe.inf.arc', 'Arcaea Infinity'),
]

def _pkg_from_path(path):
    """从任意路径字符串里提取包名（优先 /data/data/<pkg>/ 结构，再回退为子串匹配）"""
    if not path:
        return None
    m = re.search(r'/data/data/([^/]+)/', str(path))
    if m:
        return m.group(1)
    for pkg, _ in KNOWN_PACKAGES:
        if pkg in str(path):
            return pkg
    return None

def detect_version(db_path=None):
    """
    检测 Arcaea 版本。
    检测来源（按优先级）：
      1. 数据库路径 / 当前工作目录 (pwd) 中的包名
      2. `pm list packages` 输出
      3. /data/data/<pkg> 目录是否存在
    返回 (版本名, 包名) 或 (None, None)
    """
    # 1. 从数据库路径 / 当前工作目录推断
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = None
    for src in (db_path, cwd):
        pkg = _pkg_from_path(src)
        if pkg:
            for kp, kname in KNOWN_PACKAGES:
                if pkg == kp:
                    return kname, kp

    # 2. pm list packages
    try:
        import subprocess
        r = subprocess.run(
            ['pm', 'list', 'packages'],
            capture_output=True, text=True, timeout=5
        )
        out = (r.stdout or '') + (r.stderr or '')
        for pkg, name in KNOWN_PACKAGES:
            if f'package:{pkg}' in out:
                return name, pkg
    except Exception:
        pass

    # 3. 直接检查 /data/data 目录
    for pkg, name in KNOWN_PACKAGES:
        if os.path.isdir(f'/data/data/{pkg}'):
            return name, pkg

    return None, None

# ========== 单曲 PTT 公式 ==========
def calculate_single_ptt(chart_constant, score):
    if score >= 10000000:
        return chart_constant + 2.0
    elif score >= 9950000:
        return chart_constant + 1.5 + (score - 9950000) / 100000.0
    elif score >= 9800000:
        return chart_constant + 1.0 + (score - 9800000) / 400000.0
    else:
        ptt = chart_constant + (score - 9500000) / 300000.0
        return max(0.0, ptt)

# ========== 数据库检测 ==========
def is_valid_arcaea_db(path):
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as f:
            if not f.read(16).startswith(b'SQLite format 3\x00'):
                return False
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scores'")
        ok = cur.fetchone() is not None
        conn.close()
        return ok
    except Exception:
        return False

def find_db(paths):
    for p in paths:
        if is_valid_arcaea_db(p):
            return p
    for p in ['st3', './st3', '/data/data/moe.inf.arc/files/st3']:
        if is_valid_arcaea_db(p):
            return p
    return None

# ========== 名称规范化与匹配 ==========
def normalize(s):
    return re.sub(r'[^a-z0-9]', '', str(s).lower())

MANUAL_MAP = {
    'ifi': '〇、',
    'eightem': '8-EM',
    # 'sacrosanct': '真实曲名',
}

def build_song_index(songlist):
    index = {}
    entries = {}
    raw_index = {}
    for k, v in songlist.items():
        n = normalize(k)
        if n:
            index[n] = k
            entries[n] = v
        raw_index[k] = v
    return index, entries, raw_index

def find_song(db_song_id, index, entries, raw_index, threshold=0.75):
    if db_song_id in MANUAL_MAP:
        target = MANUAL_MAP[db_song_id]
        if target in raw_index:
            return target, raw_index[target]
        n_target = normalize(target)
        if n_target in entries:
            return index[n_target], entries[n_target]

    n = normalize(db_song_id)
    if not n:
        if db_song_id in raw_index:
            return db_song_id, raw_index[db_song_id]
        return None, None

    if n in entries:
        return index[n], entries[n]

    best_substr = None
    best_substr_len = 0
    for jn, entry in entries.items():
        if len(jn) >= 4 and re.search(re.escape(jn), n):
            if len(jn) > best_substr_len:
                best_substr = (index[jn], entry)
                best_substr_len = len(jn)
        elif len(n) >= 4 and re.search(re.escape(n), jn):
            if len(n) > best_substr_len:
                best_substr = (index[jn], entry)
                best_substr_len = len(n)
    if best_substr:
        return best_substr

    best_prefix = None
    best_diff = 999
    for jn, entry in entries.items():
        if jn.startswith(n) or n.startswith(jn):
            diff = abs(len(jn) - len(n))
            if diff < best_diff:
                best_diff = diff
                best_prefix = (index[jn], entry)
    if best_prefix:
        return best_prefix

    matches = difflib.get_close_matches(n, list(entries.keys()), n=1, cutoff=threshold)
    if matches:
        m = matches[0]
        return index[m], entries[m]

    return None, None

# ========== 难度查询：精确优先 + 回退 ==========
def get_constant(entry, difficulty):
    """
    返回 (定数, 是否回退, 实际使用的难度键)
    - difficulty=3 (BYD): 先 "3"，再 "4"
    - difficulty=4 (ETR): 先 "4"，再 "3"
    - 其他难度直接匹配
    """
    if entry is None:
        return None, False, None

    if difficulty in (3, 4, '3', '4'):
        # 精确键
        exact = '3' if str(difficulty) in ('3',) else '4'
        alt = '4' if exact == '3' else '3'
        # 先精确
        if exact in entry:
            v = entry[exact]
            if isinstance(v, (int, float)):
                return float(v), False, exact
        # 再回退
        if alt in entry:
            v = entry[alt]
            if isinstance(v, (int, float)):
                return float(v), True, alt
        return None, False, None

    # 其他难度
    for key in (str(difficulty), difficulty):
        if key in entry:
            v = entry[key]
            if isinstance(v, (int, float)):
                return float(v), False, str(key)
    return None, False, None

def diff_label(difficulty):
    d = str(difficulty)
    if d == '3':
        return 'Beyond'
    if d == '4':
        return 'Eternal'
    names = {'0': 'Past', '1': 'Present', '2': 'Future'}
    return names.get(d, d)

# ========== 主程序 ==========
def main():
    parser = argparse.ArgumentParser(description="计算 Arcaea 新版 PTT (B50 + B10)")
    parser.add_argument("paths", nargs="*", help="数据库路径")
    parser.add_argument("--songlist", "-s", required=True, help="谱面定数 JSON")
    parser.add_argument("--debug", "-d", action="store_true", help="打印匹配详情")
    parser.add_argument("--version", "-v", action="version",
                        version=f"arcaeaptt {VERSION}",
                        help="显示版本号并退出")
    args = parser.parse_args()

    db_path = find_db(args.paths)
    if not db_path:
        print("错误：未找到有效的 Arcaea 数据库")
        sys.exit(1)
    print(f"arcaeaptt v{VERSION}")
    print(f"使用数据库: {db_path}")
    version_name, pkg_name = detect_version(db_path)
    if version_name:
        print(f"检测到版本: {version_name} ({pkg_name})")
    else:
        print("检测到版本: 未知（未识别到已知 Arcaea 包名）")

    with open(args.songlist, 'r', encoding='utf-8') as f:
        songlist = json.load(f)
    index, entries, raw_index = build_song_index(songlist)
    print(f"定数表歌曲数: {len(songlist)}（有效规范化键 {len(entries)}）")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(scores)")
    columns = [col[1] for col in cursor.fetchall()]

    song_id_col = difficulty_col = score_col = None
    for col in columns:
        cl = col.lower()
        if cl == 'songid' or (('song' in cl) and ('id' in cl)):
            song_id_col = col
        elif cl == 'songdifficulty' or ('difficulty' in cl):
            difficulty_col = col
        elif cl == 'score':
            score_col = col

    if not all([song_id_col, difficulty_col, score_col]):
        print("错误：无法识别必要列。")
        sys.exit(1)

    query = f"""
        SELECT {song_id_col}, {difficulty_col}, MAX({score_col}) as max_score
        FROM scores
        GROUP BY {song_id_col}, {difficulty_col}
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    print(f"数据库记录数（去重后）: {len(rows)}")

    results = []
    fallback_list = []          # 用了回退的成绩
    unmatched_song = []
    unmatched_diff = []
    for song_id, difficulty, score in rows:
        if score is None:
            continue
        jkey, entry = find_song(song_id, index, entries, raw_index)
        if entry is None:
            unmatched_song.append((song_id, difficulty, score))
            continue
        constant, is_fallback, used_key = get_constant(entry, difficulty)
        if constant is None:
            unmatched_diff.append((song_id, difficulty, score, jkey, entry))
            continue
        ptt = calculate_single_ptt(constant, score)
        rec = {
            'db_song_id': song_id,
            'json_key': jkey,
            'difficulty': difficulty,
            'diff_name': diff_label(difficulty),
            'used_key': used_key,
            'is_fallback': is_fallback,
            'score': score,
            'constant': constant,
            'ptt': ptt,
        }
        results.append(rec)
        if is_fallback:
            fallback_list.append(rec)

    matched = len(results)
    print(f"\n成功匹配定数的成绩数: {matched}")
    print(f"未匹配歌曲: {len(unmatched_song)} 条")
    print(f"缺少难度定数: {len(unmatched_diff)} 条")
    print(f"回退匹配（BYD/ETR 混用）: {len(fallback_list)} 条")

    if args.debug or fallback_list or unmatched_song or unmatched_diff:
        print("\n匹配详情：")
        for r in results:
            tag = " [回退]" if r['is_fallback'] else ""
            print(f"  {r['db_song_id']:<25} -> {r['json_key']:<40} | 难度 {r['difficulty']} ({r['diff_name']}) | 用键 \"{r['used_key']}\" | 定数 {r['constant']} | 分数 {r['score']}{tag}")

        if fallback_list:
            print(f"\n【回退匹配】{len(fallback_list)} 条（数据库难度与 JSON 键不一致，用了另一个难度的定数）：")
            for r in fallback_list:
                print(f"  {r['db_song_id']:<25} -> {r['json_key']:<40} | 数据库难度 {r['difficulty']} ({r['diff_name']}) | 实际用了 \"{r['used_key']}\" 定数 {r['constant']} | 分数 {r['score']}")

        if unmatched_diff:
            print(f"\n【缺少难度定数】{len(unmatched_diff)} 条：")
            for s, d, sc, jkey, entry in unmatched_diff:
                avail = sorted(entry.keys())
                print(f"  songId={s:<22} -> {jkey:<40} | 难度 {d} ({diff_label(d)}) 缺失 | JSON 可用键: {avail} | 分数 {sc}")

        if unmatched_song:
            print(f"\n【歌曲未匹配】{len(unmatched_song)} 条：")
            for s, d, sc in unmatched_song:
                print(f"  songId={s}, difficulty={d}, score={sc}")

    if not results:
        print("\n⚠️ 没有任何成绩匹配到定数。")
        sys.exit(0)

    results.sort(key=lambda x: x['ptt'], reverse=True)
    best_50 = results[:50]
    best_10 = results[:10]

    b50_sum = sum(r['ptt'] for r in best_50)
    b10_sum = sum(r['ptt'] for r in best_10)
    total_ptt = (b50_sum + 2 * b10_sum) / 60.0

    print("\n" + "="*60)
    print(f"总 PTT: {total_ptt:.4f}")
    print(f"Best 50 数量: {len(best_50)}")
    print(f"Best 10 数量: {len(best_10)}")
    print("="*60)

    print("\n前 10 成绩：")
    print(f"{'排名':<4} {'PTT':<9} {'定数':<6} {'分数':<10} {'歌曲':<40} {'难度'}")
    for i, r in enumerate(best_10, 1):
        tag = "*" if r['is_fallback'] else " "
        print(f"{i:<4} {r['ptt']:<9.4f} {r['constant']:<6.1f} {r['score']:<10} {r['json_key']:<40} {r['diff_name']}{tag}")

    print("\nBest 50 列表（* = 回退匹配）：")
    for i, r in enumerate(best_50, 1):
        tag = " *" if r['is_fallback'] else ""
        print(f"{i:<3} PTT: {r['ptt']:.4f} | 定数: {r['constant']:.1f} | 分数: {r['score']} | 歌曲: {r['json_key']} | 难度: {r['diff_name']}{tag}")

if __name__ == "__main__":
    main()
