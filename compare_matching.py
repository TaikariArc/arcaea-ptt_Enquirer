import importlib.util
import json
import sqlite3
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


old = load('oldmod', 'arcaeaptt.py')
new = load('newmod', 'arcaeaptt_refactored.py')

songs = json.load(open('chart_constant.json'))
idx, entries, raw = old.build_song_index(songs)
matcher = new.SongMatcher(songs)

con = sqlite3.connect('/tmp/st3_test')
ids = sorted({r[0] for r in con.execute('SELECT DISTINCT songId FROM scores')})
diff = 0
for sid in ids:
    a = old.find_song(sid, idx, entries, raw)
    b = matcher.find(sid)
    same = (a[0] == b[0]) and (
        (a[1] is None and b[1] is None) or (a[1] == b[1])
    )
    if not same:
        diff += 1
        print('DIFF', repr(sid), 'old->', a[0], 'new->', b[0])
print('total ids', len(ids), 'diffs', diff)

# 额外：随机脏名字压力对比
import random
random.seed(1)
alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789-_ !@#'
sample_keys = list(songs.keys())
bad = 0
for _ in range(3000):
    if random.random() < 0.5:
        k = random.choice(sample_keys)
        k = k[: max(1, random.randint(1, len(k)))]
    else:
        k = ''.join(random.choice(alphabet) for _ in range(random.randint(0, 12)))
    a = old.find_song(k, idx, entries, raw)
    b = matcher.find(k)
    same = (a[0] == b[0]) and ((a[1] is None and b[1] is None) or (a[1] == b[1]))
    if not same:
        bad += 1
        if bad < 15:
            print('FUZZDIFF', repr(k), 'old->', a[0], 'new->', b[0])
print('fuzz diffs', bad)
