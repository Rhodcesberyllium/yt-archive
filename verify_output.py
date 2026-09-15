# -*- coding: utf-8 -*-
"""
verify_output.py — 对抓取产物做验收自检（增强版：默认连带校验同目录所有频道清单）。

检查项:
  0. 文件编码为 UTF-8
  1. 每条视频链接格式: https://www.youtube.com/watch?v=<11位ID>
  2. 视频ID无重复；条数与头部"视频总数(去重后)"一致
  3. 头部覆盖率信息（频道计数 vs 抓取条数）解析并打印
  4. 无"伪精确"占位日期：老版 `YYYY-MM-DD (UTC+8, 近似值)` 格式出现即失败
  5. 日期按真实精度分类统计（精确秒 / 日 / 月 / 年 / 未知）
  6. 排序断言：精确与日精度条目之间必须严格从新到旧（硬失败）；
     涉及月/年推断条目的顺序异常仅软警告（推断锚点可能跨真实边界）
  7. 标题"待补"数量与日期"未知"数量报告（--strict 时作为失败条件）
  8. 最新一条与频道 RSS 缓存对照（标题+日期）
  9. 可靠日期占比（精确到秒或日）报告

用法:
    python verify_output.py data/NurdRage_videos.txt [--rss <rss.xml>] [--strict]
    python verify_output.py --all                 # 校验 data/ 下所有频道清单

说明：不给 --all 时，除了指定的那份，还会**连带校验同目录下其它 *_videos.txt**，
任一失败即整体失败——这样工作流里只传一个文件也能覆盖全部频道。
"""
import glob
import os
import re
import sys
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
SOLID_RE = re.compile(r"可靠日期\(精确到秒或日\)合计: (\d+) / (\d+) 条 = (\d+)%")


def parse_dt(s, fmt):
    return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ)


def month_anchor(y, m):
    return datetime(y, m, 15, tzinfo=CN_TZ)


def year_anchor(y):
    return datetime(y, 7, 1, tzinfo=CN_TZ)


def verify_one(path, rss_path=None, strict=False):
    """校验单个文件。全部检查项通过返回 True，否则 False（不再直接退出进程）。"""
    if not os.path.isfile(path):
        print("[X] 文件不存在: %s" % path)
        return False
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8")          # 检查项 0
    except UnicodeDecodeError as ex:
        print("[X] 非 UTF-8 编码: %s" % ex)
        return False
    print("[0] UTF-8 解码: 通过（%d 字节）" % len(raw))

    cm = COVER_RE.search(text)
    coverage = None
    if cm:
        coverage = int(cm.group(3))
        print("[3] 覆盖率: 频道计数 %s，抓取 %s 条，覆盖率 %d%%"
              % (cm.group(1), cm.group(2), coverage))
    else:
        print("[3] 覆盖率: 输出头部未含频道计数行（旧版输出？）")

    entries, cur, bad_approx = [], None, 0
    for line in text.splitlines():
        m = re.match(r"【(\d+)】视频名称: (.*)", line)
        if m:
            cur = {"idx": int(m.group(1)), "title": m.group(2), "link": None,
                   "dt": None, "level": None}
            entries.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("视频链接: "):
            url = s[len("视频链接: "):]
            mm = LINK_RE.match(url)
            if not mm:
                print("[X] 链接格式错误: %s" % url)
                return False
            cur["link"] = mm.group(1)
        elif s.startswith("发布时间: "):
            mm = EXACT_RE.match(s)
            if mm:
                cur["dt"] = parse_dt(mm.group(1), "%Y-%m-%d %H:%M:%S")
                cur["level"] = 0            # 精确到秒（含"来自镜像/接口"的写法）
            elif BAD_APPROX_RE.match(s):
                bad_approx += 1             # 伪精确占位，见检查项 4
            else:
                md = DAY_RE.match(s)
                if md:
                    cur["dt"] = parse_dt(md.group(1), "%Y-%m-%d")
                    cur["level"] = 1        # 日精度
                else:
                    mm2 = MONTH_RE.match(s)
                    if mm2:
                        cur["dt"] = month_anchor(int(mm2.group(1)), int(mm2.group(2)))
                        cur["level"] = 2    # 月精度
                    else:
                        my = YEAR_RE.match(s)
                        if my:
                            cur["dt"] = year_anchor(int(my.group(1)))
                            cur["level"] = 3  # 年精度
                        elif UNKNOWN_RE.match(s):
                            cur["level"] = 4  # 未知（可带 ID 时序区间提示）
                        else:
                            print("[?] 未识别的发布时间行: %s" % line)
                            return False

    if not entries:
        print("[X] 未解析到任何条目，疑似文件被截断或格式错误")
        return False

    if bad_approx:
        print("[X] 发现 %d 条伪精确占位日期（格式: YYYY-MM-DD (UTC+8, 近似值)），禁止出现！"
              % bad_approx)
        return False
    print("[4] 伪精确占位日期: 0 条 ✓")

    links = [e["link"] for e in entries if e["link"]]
    if len(links) != len(entries):
        print("[X] 链接缺失: %d/%d 条" % (len(entries) - len(links), len(entries)))
        return False
    dup = len(links) - len(set(links))
    print("[1] 链接格式: 全部通过（%d 条）" % len(links))
    print("[2] 视频ID去重: %s（重复 %d 条）" % ("通过" if dup == 0 else "失败", dup))
    if dup:
        return False

    tm = TOTAL_RE.search(text)
    total_hdr = int(tm.group(1)) if tm else None
    if total_hdr is not None and total_hdr != len(entries):
        print("[X] 条数不一致: 头部声明 %s，实际条目 %d" % (total_hdr, len(entries)))
        return False
    print("[2] 条数一致: 头部 %s = 实际 %d ✓" % (total_hdr or len(entries), len(entries)))

    cnt = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for e in entries:
        if e["level"] is not None:
            cnt[e["level"]] += 1
    print("[5] 日期分布: 精确秒 %d | 日精度 %d | 月精度 %d | 年精度 %d | 未知 %d"
          % (cnt[0], cnt[1], cnt[2], cnt[3], cnt[4]))

    sm = SOLID_RE.search(text)
    if sm:
        print("[9] 可靠日期(精确秒或日): %s / %s 条 = %s%%"
              % (sm.group(1), sm.group(2), sm.group(3)))

    known = [(i, e["dt"], e["level"]) for i, e in enumerate(entries) if e["dt"] is not None]
    hard_bad, soft_bad = [], []
    for a, b in zip(known, known[1:]):
        if a[1] < b[1]:
            if max(a[2], b[2]) <= 1:
                hard_bad.append((a[0] + 1, b[0] + 1))
            else:
                soft_bad.append((a[0] + 1, b[0] + 1, a[2], b[2]))
    if hard_bad:
        print("[6] 排序失败(精确/日精度区间乱序): %s（前3处）" % hard_bad[:3])
        return False
    print("[6] 排序断言: 精确/日精度区间从新到旧 ✓（涉及月/年推断锚点的软警告 %d 处%s）"
          % (len(soft_bad), ("，如 %s" % soft_bad[:2]) if soft_bad else ""))

    pend = sum(1 for e in entries if "(标题待补)" in e["title"])
    print("[7] 标题待补: %d 条%s" % (pend, "（建议继续重跑补全）" if pend else " ✓"))
    print("[7] 日期未知: %d 条%s" % (cnt[4], "（会由后续运行逐周补全）" if cnt[4] else " ✓"))
    if strict and (pend or cnt[4]):
        print("[X] --strict: 存在标题待补或日期未知，判定失败。")
        return False
    if coverage is not None and coverage < 100:
        print("[3] 覆盖率未达 100%%（%d%%），存在缺口，可继续重跑补全。" % coverage)

    rss_candidates = [rss_path] if rss_path else []
    if os.path.isdir("cache"):
        rss_candidates += [os.path.join("cache", d, "pages", "rss.xml")
                           for d in os.listdir("cache")
                           if os.path.isdir(os.path.join("cache", d))]
    for rp in rss_candidates:
        if os.path.isfile(rp):
            body = open(rp, "rb").read()
            mm = re.search(rb"<entry>.*?<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)"
                           rb"</published>.*?<title>([^<]+)</title>", body, re.S)
            if mm:
                vid, pub, title = mm.groups()
                pub_dt = datetime.fromisoformat(pub.decode().replace("Z", "+00:00")) \
                    .astimezone(CN_TZ)
                print("[8] RSS 对照: 最新视频 %s | %s"
                      % (title.decode()[:60], pub_dt.strftime("%Y-%m-%d %H:%M:%S")))
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
    return True


def main():
    args = sys.argv[1:]
    path, rss_path, strict, all_files = None, None, False, False
    for i, a in enumerate(args):
        if a == "--rss" and i + 1 < len(args):
            rss_path = args[i + 1]
        elif a == "--strict":
            strict = True
        elif a == "--all":
            all_files = True
        elif not a.startswith("--") and path is None:
            path = a

    if all_files or (path and os.path.isdir(path)):
        base = path or "data"
        targets = sorted(p for p in glob.glob(os.path.join(base, "*_videos.txt"))
                         if not os.path.basename(p).startswith("_"))
    else:
        if not path:
            path = "NurdRage_videos.txt"
        targets = [path]
        # 连带校验同目录下其它频道清单：工作流只传一个文件，也能覆盖全部频道
        d = os.path.dirname(path) or "."
        seen = {os.path.abspath(path)}
        for p in sorted(glob.glob(os.path.join(d, "*_videos.txt"))):
            if (os.path.abspath(p) in seen or os.path.basename(p).startswith("_")
                    or not os.path.isfile(p)):
                continue
            seen.add(os.path.abspath(p))
            targets.append(p)

    if not targets:
        print("[X] 没有找到任何待校验的清单文件")
        sys.exit(1)

    results = []
    for i, p in enumerate(targets, 1):
        print("\n" + "=" * 60)
        print("== 校验 %d/%d: %s ==" % (i, len(targets), p))
        print("=" * 60)
        ok = verify_one(p, rss_path=rss_path, strict=strict)
        results.append((p, ok))

    bad = [p for p, ok in results if not ok]
    print("\n" + "=" * 60)
    if bad:
        print("== 总结: 失败 ✗（%d/%d 个文件未通过）==" % (len(bad), len(results)))
        for p in bad:
            print("   - %s" % p)
        sys.exit(1)
    print("== 总结: 全部通过 ✓（%d 个文件）==" % len(results))
    sys.exit(0)


if __name__ == "__main__":
    main()
