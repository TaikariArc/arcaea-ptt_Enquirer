# arcaea-ptt_Enquirer
用于arcaea私有目录的sqlite数据库查询并计算ptt

本地离线计算 Arcaea 新版 PTT（B50 + B10）。

读取游戏数据库 st3 与定数表 chart_constant.json，匹配成绩后计算单曲 PTT 并汇总。无需联网。

功能

· 自动查找 Arcaea 数据库（原版 / Infinity）(需要root权限)
· 自动识别版本（moe.low.arc / moe.inf.arc）
· 曲名模糊匹配（精确 / 子串 / 前缀 / difflib）
· 难度回退（BYD ↔ ETR）
· 输出 Best 50 + Best 10 与总 PTT
· 支持 --debug 查看匹配详情

环境

· Python 3.7+
· 仅使用标准库

使用

```bash
python3 arcaeaptt.py st3 -s chart_constant.json
```

参数

参数 说明
paths 数据库路径（可选，自动查找）
-s, --songlist 定数表 JSON（必填）
-d, --debug 打印匹配详情
-v, --version 显示版本号
-h, --help 显示帮助

自动查找路径

1. st3
2. ./st3
3. /data/data/moe.inf.arc/files/st3
4. /data/data/moe.low.arc/files/st3

定数表格式

```json
{
  "Arcana Eden": { "2": 10.5, "3": 11.6 },
  "Testify":     { "2": 10.9, "3": 12.0 }
}
```

难度键：0 Past / 1 Present / 2 Future / 3 Beyond / 4 Eternal

输出示例

```
arcaeaptt v0.0.2
使用数据库: st3
检测到版本: Arcaea Infinity (moe.inf.arc)
定数表歌曲数: 550（有效规范化键 544）
数据库记录数（去重后）: 50

成功匹配定数的成绩数: 50
未匹配歌曲: 0 条
缺少难度定数: 0 条
回退匹配（BYD/ETR 混用）: 0 条

============================================================
总 PTT: 13.6534
Best 50 数量: 50
Best 10 数量: 10
============================================================
```

PTT 公式

分数区间 单曲 PTT
≥ 10,000,000 定数 + 2.0
≥ 9,950,000 定数 + 1.5 + (分数 − 9,950,000) / 100,000
≥ 9,800,000 定数 + 1.0 + (分数 − 9,800,000) / 400,000
< 9,800,000 定数 + (分数 − 9,500,000) / 300,000（≥ 0）

总 PTT：

```
(Best 50 之和 + 2 × Best 10 之和) / 60
```

从源码构建（Nuitka）

```bash
pkg install clang patchelf binutils ccache
pip install nuitka ordered-set zstandard

python3 -m nuitka --onefile --standalone \
  --assume-yes-for-downloads \
  --output-filename=arcaeaptt \
  --remove-output \
  arcaeaptt.py
```

生成单文件 arcaeaptt（Termux / Linux 环境）。

手动映射

曲名差异过大时可加入 MANUAL_MAP：

```python
MANUAL_MAP = {
    'ifi': '〇、',
    'eightem': '8-EM',
}
```

键为数据库 songId，值为 JSON 中的原始键名。

版本历史

版本 说明
0.0.2 版本检测、--version、Nuitka 构建
0.0.1 初始版本，B50 + B10

致谢
由deepseek编写

许可

仅供个人学习与自用。Arcaea 相关名称与内容版权归 lowiro 所有。
