# -*- coding: utf-8 -*-
"""
verify_output.py — 对抓取产物做验收自检（默认连带校验同目录所有频道清单）。

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
  9. 可靠日期占比（精确到秒或日）报告，可用 --min-reliable 设成硬门槛
 10. 数据来源明细（各通道各贡献多少条）

顺带产出:
  data/_progress.txt —— 各频道补齐进度汇总（每次校验后自动刷新，
  一眼看出每个频道还缺多少条、谁会被下一轮优先抓）

用法:
    python verify_output.py data/NurdRage_videos.txt [--rss <rss.xml>] [--strict]
    python verify_output.py --all                          # 校验 data/ 下所有频道清单
    python verify_output.py --all --min-reliable 90        # 可靠日期占比低于 90% 即失败

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
SRC_RE = re.compile(r"^- 最终各来源条数: (.+)$", re.M)


def _channel_of(path):
    """从产物头部取频道名（用于进度汇总显示）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("频道: "):
                    return line[len("频道: "):].strip()
                if line.startswith("【") or line.startswith("----"):
                    break
    except Exception:
        pass
    return os.path.basename(path).replace("_videos.txt", "")


def _stats(path, total=0, solid=0, unknown=0, ratio=None, channel=""):
    """把单文件校验结果整理成统计字典，供汇总与进度文件使用。"""
    return {"path": path, "channel": channel, "total": total or 0,
            "solid": solid or 0, "unknown": unknown or 0, "ratio": ratio,
            "gap": max(0, (total or 0) - (solid or 0))}


def parse_dt(s, fmt):
    return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ)


def month_anchor(y, m):
    return datetime(y, m, 15, tzinfo=CN_TZ)


def year_anchor(y):
    return datetime(y, 7, 1, tzinfo=CN_TZ)


def verify_one(path, rss_path=None, strict=False, min_reliable=None):
    """校验单个文件。返回 (是否通过, 统计字典)。统计字典含 total/solid/unknown/ratio/gap。"""
    if not os.path.isfile(path):
        print("[X] 文件不存在: %s" % path)
        return False, None
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8")          # 检查项 0
    except UnicodeDecodeError as ex:
        print("[X] 非 UTF-8 编码: %s" % ex)
        return False, None
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
                return False, _stats(path)
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
                            return False, _stats(path)

    if not entries:
        print("[X] 未解析到任何条目，疑似文件被截断或格式错误")
        return False, _stats(path)

    if bad_approx:
        print("[X] 发现 %d 条伪精确占位日期（格式: YYYY-MM-DD (UTC+8, 近似值)），禁止出现！"
              % bad_approx)
        return False, _stats(path)
    print("[4] 伪精确占位日期: 0 条 ✓")

    links = [e["link"] for e in entries if e["link"]]
    if len(links) != len(entries):
        print("[X] 链接缺失: %d/%d 条" % (len(entries) - len(links), len(entries)))
        return False, _stats(path)
    dup = len(links) - len(set(links))
    print("[1] 链接格式: 全部通过（%d 条）" % len(links))
    print("[2] 视频ID去重: %s（重复 %d 条）" % ("通过" if dup == 0 else "失败", dup))
    if dup:
        return False, _stats(path)

    tm = TOTAL_RE.search(text)
    total_hdr = int(tm.group(1)) if tm else None
    if total_hdr is not None and total_hdr != len(entries):
        print("[X] 条数不一致: 头部声明 %s，实际条目 %d" % (total_hdr, len(entries)))
        return False, _stats(path)
    print("[2] 条数一致: 头部 %s = 实际 %d ✓" % (total_hdr or len(entries), len(entries)))

    cnt = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for e in entries:
        if e["level"] is not None:
            cnt[e["level"]] += 1
    print("[5] 日期分布: 精确秒 %d | 日精度 %d | 月精度 %d | 年精度 %d | 未知 %d"
          % (cnt[0], cnt[1], cnt[2], cnt[3], cnt[4]))

    ratio = None
    sm = SOLID_RE.search(text)
    if sm:
        ratio = int(sm.group(3))
        print("[9] 可靠日期(精确秒或日): %s / %s 条 = %s%%"
              % (sm.group(1), sm.group(2), sm.group(3)))
    else:
        print("[9] 输出里没有“可靠日期”统计行（旧版输出？）")
    st = _stats(path, len(entries), cnt[0] + cnt[1], cnt[4], ratio, _channel_of(path))

    src = SRC_RE.search(text)
    if src:
        print("[10] 数据来源明细: %s" % src.group(1).strip())

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
        return False, st
    print("[6] 排序断言: 精确/日精度区间从新到旧 ✓（涉及月/年推断锚点的软警告 %d 处%s）"
          % (len(soft_bad), ("，如 %s" % soft_bad[:2]) if soft_bad else ""))

    pend = sum(1 for e in entries if "(标题待补)" in e["title"])
    print("[7] 标题待补: %d 条%s" % (pend, "（建议继续重跑补全）" if pend else " ✓"))
    print("[7] 日期未知: %d 条%s" % (cnt[4], "（会由后续运行逐轮补全）" if cnt[4] else " ✓"))
    if strict and (pend or cnt[4]):
        print("[X] --strict: 存在标题待补或日期未知，判定失败。")
        return False, st
    if coverage is not None and coverage < 100:
        print("[3] 覆盖率未达 100%%（%d%%），存在缺口，可继续重跑补全。" % coverage)

    if min_reliable is not None:
        if ratio is None:
            print("[X] --min-reliable: 文件里没有可靠日期统计，无法判定。")
            return False, st
        if ratio < min_reliable:
            print("[X] --min-reliable %d%%: 当前可靠日期仅 %d%%，判定失败。" % (min_reliable, ratio))
            return False, st
        print("[9] --min-reliable %d%%: 当前 %d%%，通过 ✓" % (min_reliable, ratio))

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
    return True, st


def write_progress(path, stats):
    """把各频道的补齐进度汇总成 data/_progress.txt（每轮自动刷新，一眼看全）。

    可靠日期 = 精确到秒或日。缺口大的频道会被下一轮优先抓（见 fetch.py 的 order_channels）。"""
    rows = [r for r in stats if r and r.get("total")]
    if not rows:
        return
    rows.sort(key=lambda r: (-r["gap"], r["channel"]))
    L = ["=" * 62,
         "各频道补齐进度（每次运行自动刷新）",
         "=" * 62,
         "生成时间: %s (UTC+8)" % datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
         "",
         "%-22s %8s %10s %7s %8s" % ("频道", "总条数", "可靠日期", "占比", "还缺"),
         "-" * 62]
    tt = ts = 0
    for r in rows:
        tt += r["total"]
        ts += r["solid"]
        L.append("%-22s %8d %10d %7s %8d"
                 % (r["channel"][:22], r["total"], r["solid"],
                    ("%.0f%%" % r["ratio"]) if r["ratio"] is not None else "-", r["gap"]))
    L.append("-" * 62)
    L.append("%-22s %8d %10d %7s %8d"
             % ("合计", tt, ts, ("%.0f%%" % (100.0 * ts / tt)) if tt else "-", tt - ts))
    L.append("")
    L.append("说明：")
    L.append("- 「可靠日期」= 精确到秒（官方 RSS / 官方接口）或精确到日（官方元数据 / 历史存档）；")
    L.append("  其余标为「推断」或「未知」，如实标注精度，绝不假装精确。")
    L.append("- 缺口最大的频道会被下一轮优先抓取。watch 页通路的配额按 IP 约每小时 200 次，")
    L.append("  因此是逐轮补齐；已补到的日期会写进缓存，不会丢。")
    L.append("- 想快点补完：在 Actions 页多点几次 Run workflow（每次间隔一小时以上），")
    L.append("  或把 .github/workflows/fetch.yml 里的 cron 临时改成每天一次，补完再改回每周。")
    L.append("=" * 62)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        print("\n进度汇总已写入 %s" % path)
    except Exception as ex:
        print("[!] 写进度汇总失败：%s" % str(ex)[:80])


def main():
    args = sys.argv[1:]
    path, rss_path, strict, all_files, min_reliable = None, None, False, False, None
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:          # 上一个是带参数的选项，这个值是它的参数，不能当成文件路径
            skip_next = False
            continue
        if a == "--rss" and i + 1 < len(args):
            rss_path = args[i + 1]
            skip_next = True
        elif a == "--strict":
            strict = True
        elif a == "--all":
            all_files = True
        elif a == "--min-reliable" and i + 1 < len(args):
            try:
                min_reliable = int(args[i + 1])
            except ValueError:
                print("[X] --min-reliable 需要一个百分数，如 --min-reliable 90")
                sys.exit(2)
            skip_next = True
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
    progress_path = os.path.join(os.path.dirname(os.path.abspath(targets[0])) or ".",
                                 "_progress.txt")

    results = []
    for i, p in enumerate(targets, 1):
        print("\n" + "=" * 60)
        print("== 校验 %d/%d: %s ==" % (i, len(targets), p))
        print("=" * 60)
        ok, st = verify_one(p, rss_path=rss_path, strict=strict,
                            min_reliable=min_reliable)
        results.append((p, ok, st))

    bad = [p for p, ok, _ in results if not ok]
    stats = [st for _, _, st in results if st is not None]
    print("\n" + "=" * 60)
    good = [st for st in stats if st.get("ratio") is not None]
    if good:
        print("== 可靠日期占比: %s =="
              % "，".join("%s %d%%" % (st["channel"] or os.path.basename(st["path"]),
                                      st["ratio"]) for st in good))
    tot = sum(st["total"] for st in stats)
    sol = sum(st["solid"] for st in stats)
    if tot:
        print("== 全部频道合计: %d / %d 条 = %.0f%%（还缺 %d 条）=="
              % (sol, tot, 100.0 * sol / tot, tot - sol))
    write_progress(progress_path, stats)

    if bad:
        print("== 总结: 失败 ✗（%d/%d 个文件未通过）==" % (len(bad), len(results)))
        for p in bad:
            print("   - %s" % p)
        sys.exit(1)
    print("== 总结: 全部通过 ✓（%d 个文件）==" % len(results))
    sys.exit(0)


if __name__ == "__main__":
    main()
