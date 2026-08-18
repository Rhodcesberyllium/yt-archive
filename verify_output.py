# -*- coding: utf-8 -*-
"""
verify_output.py — 对 youtube_fetcher.py 的输出 txt 做验收自检（增强版）。

检查项:
  0. 文件编码为 UTF-8
  1. 每条视频链接格式: https://www.youtube.com/watch?v=<11位ID>
  2. 视频ID无重复；条数与头部"视频总数(去重后)"一致
  3. 头部覆盖率信息（频道页计数 vs 抓取条数）解析并打印
  4. 无"伪精确"占位日期：老版 `YYYY-MM-DD (UTC+8, 近似值)` 格式出现即失败
  5. 日期按真实精度分类统计（精确秒 / 日 / 月 / 年 / 未知）
  6. 排序断言：精确与日精度条目之间必须严格从新到旧（硬失败）；
     涉及月/年推断条目的顺序异常仅软警告（推断锚点可能跨真实边界）
  7. 标题"待补"数量与日期"未知"数量报告（--strict 时作为失败条件）
  8. 最新一条与频道 RSS 缓存对照（标题+日期）

用法:
    python verify_output.py NurdRage_videos.txt [--rss <rss.xml>] [--strict]
"""
import re
import sys
import os
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CN_TZ = timezone(timedelta(hours=8))

LINK_RE = re.compile(r"^https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})$")
EXACT_RE = re.compile(r"^发布时间: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \(UTC\+8\)")
BAD_APPROX_RE = re.compile(r"^发布时间: \d{4}-\d{2}-\d{2} \(UTC\+8, 近似值\)")
DAY_RE = re.compile(r"^发布时间: (\d{4}-\d{2}-\d{2})（(?:推断日期|日精度)")
MONTH_RE = re.compile(r"^发布时间: (\d{4})-(\d{2})（")
YEAR_RE = re.compile(r"^发布时间: (\d{4})年（")
UNKNOWN_RE = re.compile(r"^发布时间: 未知")
COVER_RE = re.compile(r"频道页显示视频总数: ([\d,]+)（本文件抓取到 (\d+) 条，覆盖率 (\d+)%")
TOTAL_RE = re.compile(r"视频总数\(去重后\): (\d+)")


def parse_dt(s, fmt):
    return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ)


def main():
    args = sys.argv[1:]
    path = args[0] if args else "NurdRage_videos.txt"
    rss_path, strict = None, False
    for i, a in enumerate(args):
        if a == "--rss" and i + 1 < len(args):
            rss_path = args[i + 1]
        elif a == "--strict":
            strict = True

    raw = open(path, "rb").read()
    text = raw.decode("utf-8")  # 非 UTF-8 会抛异常 -> 检查项0
    print("[0] UTF-8 解码: 通过（%d 字节）" % len(raw))

    # 头部覆盖率
    cm = COVER_RE.search(text)
    tm = TOTAL_RE.search(text)
    coverage = None
    if cm:
        coverage = int(cm.group(3))
        print("[3] 覆盖率: 频道页计数 %s，抓取 %s 条，覆盖率 %d%%"
              % (cm.group(1), cm.group(2), coverage))
    else:
        print("[3] 覆盖率: 输出头部未含频道计数行（旧版输出？）")

    # 逐条目解析
    entries = []
    cur = None
    bad_approx = 0
    for line in text.splitlines():
        m = re.match(r"【(\d+)】视频名称: (.*)", line)
        if m:
            cur = {"idx": int(m.group(1)), "title": m.group(2),
                   "link": None, "dt": None, "level": None}
            entries.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("视频链接: "):
            url = s[len("视频链接: "):]
            if not LINK_RE.match(url):
                print("[X] 链接格式错误: %s" % url)
                sys.exit(1)
            cur["link"] = LINK_RE.match(url).group(1)
        elif s.startswith("发布时间: "):
            mm = EXACT_RE.match(s)
            if mm:
                cur["dt"] = parse_dt(mm.group(1), "%Y-%m-%d %H:%M:%S")
                cur["level"] = 0          # 精确到秒
            elif BAD_APPROX_RE.match(s):
                bad_approx += 1           # 伪精确占位，见检查项4
            else:
                md = DAY_RE.match(s)
                if md:
                    cur["dt"] = parse_dt(md.group(1), "%Y-%m-%d")
                    cur["level"] = 1      # 日精度（标题推断或实例原文）
                else:
                    mm2 = MONTH_RE.match(s)
                    if mm2:
                        cur["dt"] = month_anchor(int(mm2.group(1)), int(mm2.group(2)))
                        cur["level"] = 2  # 月精度
                    else:
                        my = YEAR_RE.match(s)
                        if my:
                            cur["dt"] = year_anchor(int(my.group(1)))
                            cur["level"] = 3  # 年精度
                        elif UNKNOWN_RE.match(s):
                            cur["level"] = 4  # 未知
                        else:
                            print("[?] 未识别的发布时间行: %s" % line)
                            sys.exit(1)

    if bad_approx:
        print("[X] 发现 %d 条伪精确占位日期（格式: YYYY-MM-DD (UTC+8, 近似值)），禁止出现！"
              % bad_approx)
        sys.exit(1)
    print("[4] 伪精确占位日期: 0 条 ✓")

    links = [e["link"] for e in entries if e["link"]]
    if len(links) != len(entries):
        print("[X] 链接缺失: %d/%d 条" % (len(entries) - len(links), len(entries)))
        sys.exit(1)
    dup = len(links) - len(set(links))
    print("[1] 链接格式: 全部通过（%d 条）" % len(links))
    print("[2] 视频ID去重: %s（重复 %d 条）" % ("通过" if dup == 0 else "失败", dup))
    if dup:
        sys.exit(1)

    total_hdr = int(tm.group(1)) if tm else None
    if total_hdr is not None and total_hdr != len(entries):
        print("[X] 条数不一致: 头部声明 %s，实际条目 %d" % (total_hdr, len(entries)))
        sys.exit(1)
    print("[2] 条数一致: 头部 %d = 实际 %d ✓" % (total_hdr or len(entries), len(entries)))

    # 日期分类统计
    cnt = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for e in entries:
        if e["level"] is not None:
            cnt[e["level"]] += 1
    print("[5] 日期分布: 精确秒 %d | 日精度 %d | 月精度 %d | 年精度 %d | 未知 %d"
          % (cnt[0], cnt[1], cnt[2], cnt[3], cnt[4]))

    # 排序断言：带时间且已知级别的相邻条目
    known = [(i, e["dt"], e["level"]) for i, e in enumerate(entries)
             if e["dt"] is not None]
    hard_bad, soft_bad = [], []
    for a, b in zip(known, known[1:]):
        if a[1] < b[1]:
            if max(a[2], b[2]) <= 1:
                hard_bad.append((a[0] + 1, b[0] + 1))
            else:
                soft_bad.append((a[0] + 1, b[0] + 1, a[2], b[2]))
    if hard_bad:
        print("[6] 排序失败(精确/日精度区间乱序): %s（前3处）" % hard_bad[:3])
        sys.exit(1)
    print("[6] 排序断言: 精确/日精度区间从新到旧 ✓（涉及月/年推断锚点的软警告 %d 处%s）"
          % (len(soft_bad), ("，如 %s" % soft_bad[:2]) if soft_bad else ""))

    # 标题待补 / 日期未知
    pend = sum(1 for e in entries if "(标题待补)" in e["title"])
    print("[7] 标题待补: %d 条%s" % (pend, "（建议继续重跑补全）" if pend else " ✓"))
    print("[7] 日期未知: %d 条%s" % (cnt[4], "（建议继续重跑补全）" if cnt[4] else " ✓"))
    if strict and (pend or cnt[4]):
        print("[X] --strict: 存在标题待补或日期未知，判定失败。")
        sys.exit(1)
    if coverage is not None and coverage < 100:
        print("[3] 覆盖率未达 100%%（%d%%），存在缺口，可通过 Wayback 补全循环缩小。" % coverage)

    # 与 RSS 缓存对照最新一条
    rss_candidates = [rss_path] if rss_path else []
    if os.path.isdir("cache"):
        rss_candidates += [os.path.join("cache", d, "pages", "rss.xml")
                           for d in os.listdir("cache")
                           if os.path.isdir(os.path.join("cache", d))]
    for rp in rss_candidates:
        if os.path.isfile(rp):
            body = open(rp, "rb").read()
            mm = re.search(rb"<entry>.*?<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)</published>.*?<title>([^<]+)</title>",
                           body, re.S)
            if mm:
                vid, pub, title = mm.groups()
                pub_dt = datetime.fromisoformat(pub.decode().replace("Z", "+00:00")).astimezone(CN_TZ)
                print("[8] RSS 对照: 最新视频 %s | %s" % (title.decode()[:60],
                                                       pub_dt.strftime("%Y-%m-%d %H:%M:%S")))
                if vid.decode() in links:
                    e = next(e for e in entries if e["link"] == vid.decode())
                    if e["dt"] is not None and e["level"] <= 1:
                        diff = abs((e["dt"] - pub_dt).total_seconds())
                        print("    输出中该视频: %s，与 RSS 差异 %.0f 秒 %s"
                              % (e["dt"].strftime("%Y-%m-%d %H:%M:%S"), diff,
                                 "（一致）" if diff < 60 else "（需核对）"))
                    else:
                        print("    输出中该视频日期精度不足（level=%s），跳过精确对照" % e["level"])
                else:
                    print("    !! RSS 最新视频不在输出中（缺口）")
            break
    else:
        print("[8] RSS 对照: 未找到 RSS 缓存，跳过")

    print("\n== 总结: %s（%d 条视频；覆盖率 %s，精确/日精度 %d 条）=="
          % ("全部通过 ✓" if not strict else "strict 通过 ✓", len(entries),
             ("%d%%" % coverage) if coverage is not None else "?" , cnt[0] + cnt[1]))
    sys.exit(0)


def month_anchor(y, m):
    return datetime(y, m, 15, tzinfo=CN_TZ)


def year_anchor(y):
    return datetime(y, 7, 1, tzinfo=CN_TZ)


if __name__ == "__main__":
    main()