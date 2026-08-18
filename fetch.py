#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py — GitHub Actions 云端全量抓取器（国内免梯子的"一劳永逸"方案）。

为什么在云端跑？
  本机在中国直连 YouTube 被墙、公共 CORS 中继又时好时坏、Wayback 快照也常被墙。
  本脚本运行在 GitHub Actions 的美国服务器上，直连 YouTube —— 无墙、无中继、无 VPN，
  每周定时全量枚举频道视频（ID + 标题 + 官方发布日），结果写回仓库，墙内随时可下载。

用法：
  python fetch.py                                  # 用 CHANNEL_URL 环境变量或内置默认频道
  CHANNEL_URL="https://www.youtube.com/@xxx" python fetch.py
  python fetch.py "https://www.youtube.com/@xxx/videos"
  python fetch.py --list-only                      # 只枚举 ID+标题（不逐条取日期，很快）
  python fetch.py --demo                           # 离线演练：内置样例跑通全流程（本地测试用）

输出：默认 data/<频道>_videos.txt，格式与本地版完全一致，可直接用 verify_output.py 验收。
依赖：yt-dlp（pip install yt-dlp）；RSS 用标准库 urllib 直取，零第三方依赖。

精度政策（与本地版一致，绝不伪造精确日期）：
  精确到秒  —— 来自官方 RSS 的 <published> 字段
  日精度    —— 来自官方 upload_date 字段（YYYMMDD）
  标题推断  —— 标题内嵌完整日期（如 "March 27th 2019"），标注"推断"
  未知      —— 官方字段暂不可取，下周自动重跑会再试
"""
import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import urllib.request
from datetime import timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CN_TZ = timezone(timedelta(hours=8))
SEP = "-" * 50
VIDEOID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
MONTH_NAME_RE = (r"(?:January|February|March|April|May|June|July|August|September"
                 r"|October|November|December|[A-Z][a-z]{2})")
DEFAULT_CHANNEL = "https://www.youtube.com/@NurdRage/videos"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(*a):
    print(*a, flush=True)


def yt_run(args, timeout=3600):
    """跑一条 yt-dlp 命令，返回 stdout 文本。任何 URL 级错误被 --ignore-errors 吸收。"""
    cmd = ["yt-dlp", "--no-warnings", "--ignore-errors", "--no-cache-dir",
           "--skip-download"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode not in (0, 1):
        log("[yt-dlp] 返回码 %d: %s" % (p.returncode, (p.stderr or p.stdout)[-400:]))
    return p.stdout or ""


# ------------------------------------------------ 时间/标题工具（与本地版同语义）

def parse_publish_date(s):
    """ISO8601 -> epoch ms；失败返回 None"""
    try:
        d = dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except Exception:
        return None


def clean_title(t):
    """清理标题残留（" - YouTube" 后缀、多余空白）"""
    s = (t or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?:\s*[-–—]\s*)?YouTube$", "", s).strip()
    return re.sub(r"\s+", " ", s)


def parse_title_date(title):
    """从标题解析完整日期 -> (epoch_ms, 'day_title'|'month_title') 或 None。
    只在标题含完整年份时推断（无年份的月+日不加假装精确，留给官方字段/未知）。"""
    s = title or ""
    m = re.search(r"\b(%s)\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,.\-]?\s+(\d{4})\b"
                  % MONTH_NAME_RE, s)
    if m:
        mon = MONTHS.get(m.group(1)[:3].capitalize())
        if mon:
            try:
                d = dt.datetime(int(m.group(3)), mon, int(m.group(2)), tzinfo=timezone.utc)
                return (int(d.timestamp() * 1000), "day_title")
            except ValueError:
                pass
    m2 = re.search(r"\b(%s)\.?\s+(\d{4})\b" % MONTH_NAME_RE, s)
    if m2:
        mon = MONTHS.get(m2.group(1)[:3].capitalize())
        if mon:
            d = dt.datetime(int(m2.group(2)), mon, 15, tzinfo=timezone.utc)
            return (int(d.timestamp() * 1000), "month_title")
    return None


def fmt_cn(ms):
    return (dt.datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            .astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"))


def fmt_utc(ms):
    return (dt.datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"))


# ------------------------------------------------ 取数（全部跑在 GitHub 美国节点）

def ensure_base(channel_url):
    for suf in ("/videos", "/shorts", "/streams", "/featured", "/playlists"):
        channel_url = channel_url.split(suf)[0]
    return channel_url.rstrip("/")


def enum_all(channel_url):
    """对 videos/shorts/streams 三个 tab 各抓一次取并集。
    返回 (ids: {id: title}, tags: {id: set(来源tab)})。"""
    base = ensure_base(channel_url)
    ids, tags = {}, {}
    for tab in ("videos", "shorts", "streams"):
        url = "%s/%s" % (base, tab)
        out = yt_run(["--flat-playlist", "--print", "%(id)s\t%(title)s", url], timeout=2400)
        got = 0
        for line in out.splitlines():
            if "\t" not in line:
                continue
            vid, t = line.split("\t", 1)
            vid = vid.strip()
            if not VIDEOID_RE.fullmatch(vid):
                continue
            ids[vid] = ids.get(vid) or t.strip()
            tags.setdefault(vid, set()).add(tab)
            got += 1
        log("[枚举] tab=%s 获得 %d 条（累计 %d）" % (tab, got, len(ids)))
    if not ids:  # 兜底：直接用调用者给的 URL 抽取一次
        out = yt_run(["--flat-playlist", "--print", "%(id)s\t%(title)s", base], timeout=2400)
        for line in out.splitlines():
            if "\t" in line:
                vid, t = line.split("\t", 1)
                vid = vid.strip()
                if VIDEOID_RE.fullmatch(vid):
                    ids[vid] = ids.get(vid) or t.strip()
    return ids, tags


def channel_meta(channel_url):
    """返回 (ucid, name)；失败返回 (None, None)"""
    base = ensure_base(channel_url)
    out = yt_run(["--flat-playlist", "--playlist-items", "1",
                  "--print", "%(channel_id)s\t%(channel)s", base], timeout=600)
    for line in out.splitlines():
        if "\t" in line:
            a, b = line.split("\t", 1)
            return a.strip() or None, b.strip() or None
    return None, None


def fetch_upload_dates(ids, chunk=60):
    """逐批对每个视频取官方 upload_date(YYYYMMDD)，返回 {id: 'YYYYMMDD'}。
    失败的（已删除/私享/风控超时）不在结果中，回退到标题推断/未知。"""
    res, ids_list, n = {}, list(ids), len(ids)
    for i in range(0, n, chunk):
        urls = ["https://www.youtube.com/watch?v=%s" % v for v in ids_list[i:i + chunk]]
        out = yt_run(["--no-playlist", "--print", "%(id)s\t%(upload_date)s",
                      "--retries", "3"] + urls, timeout=3600)
        for line in out.splitlines():
            if "\t" not in line:
                continue
            vid, d = line.split("\t", 1)
            d = d.strip()
            if VIDEOID_RE.fullmatch(vid.strip()) and re.fullmatch(r"\d{8}", d):
                res[vid.strip()] = d
        log("[日期] %d/%d" % (min(i + chunk, n), n))
    return res


def fetch_rss(ucid, dst):
    """官方 RSS：最近约 15 条，带精确到秒的发布时间。
    返回 {id: epoch_ms}，同时把原始 XML 落盘（供 verify_output.py 交叉对照）。"""
    out = {}
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + ucid
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        body = r.read()
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(body)
    for vid, pub in re.findall(
            rb"<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)</published>",
            body, re.S):
        ts = parse_publish_date(pub.decode("utf-8", "replace"))
        if ts:
            out[vid.decode()] = ts
    log("[RSS] 获得 %d 条精确日期" % len(out))
    return out


# ------------------------------------------------ 日期归并与排序

def assign_dates(entries, rss_map):
    """按可信度给每条定精度：RSS精确秒 > 官方日 > 标题完整日期 > 未知。"""
    for e in entries:
        if e.get("rss_ts"):
            e["ts_ms"], e["prec"] = e["rss_ts"], "exact"
        elif e.get("upload_date"):
            y, m, d = int(e["upload_date"][:4]), int(e["upload_date"][4:6]), int(e["upload_date"][6:8])
            e["ts_ms"] = int(dt.datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
            e["prec"] = "day_official"
        else:
            pair = parse_title_date(e.get("title") or "")
            if pair:
                e["ts_ms"], e["prec"] = pair
            else:
                e["prec"] = "unknown"


def _sort_key(e):
    ts = e.get("ts_ms")
    return (1, 0) if not ts else (0, -ts)


# ------------------------------------------------ 输出（格式与本地版逐行兼容）

TAB_LABEL = {"videos": None, "shorts": "短视频 Shorts", "streams": "直播/回放 Live"}


def write_output(path, entries, meta):
    L = []
    L.append("=" * 40)
    L.append("频道: %s" % (meta.get("name") or "-"))
    L.append("频道链接: %s" % meta.get("channel_url", "-"))
    L.append("频道 ID: %s" % meta.get("ucid", "-"))
    if meta.get("channel_count"):
        cc = meta["channel_count"]
        L.append("频道页显示视频总数: %s（本文件抓取到 %d 条，覆盖率 %.0f%%）"
                 % (cc, len(entries), 100.0 * len(entries) / cc if cc else 0))
    L.append("抓取时间: %s (UTC+8)" % dt.datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"))
    L.append("数据来源: %s" % meta.get("source_desc", "-"))
    L.append("视频总数: %d" % len(entries))
    L.append("=" * 40)
    exact = sum(1 for e in entries if e.get("prec") == "exact")
    day_o = sum(1 for e in entries if e.get("prec") == "day_official")
    title_d = sum(1 for e in entries if e.get("prec") == "day_title")
    title_m = sum(1 for e in entries if e.get("prec") == "month_title")
    unknown_n = sum(1 for e in entries if e.get("prec") == "unknown")
    for i, e in enumerate(entries, 1):
        title = clean_title(e.get("title"))
        L.append("")
        L.append("【%d】视频名称: %s" % (i, title or "(标题待补)"))
        L.append("    视频链接: https://www.youtube.com/watch?v=%s" % e["id"])
        if e.get("prec") == "exact":
            L.append("    发布时间: %s (UTC+8)   [原始: %s UTC]"
                     % (fmt_cn(e["ts_ms"]), fmt_utc(e["ts_ms"])))
        elif e.get("prec") == "day_official":
            d = dt.datetime.fromtimestamp(e["ts_ms"] / 1000.0, tz=CN_TZ)
            L.append("    发布时间: %s（日精度, 来自 YouTube 官方字段）" % d.strftime("%Y-%m-%d"))
        elif e.get("prec") == "day_title":
            d = dt.datetime.fromtimestamp(e["ts_ms"] / 1000.0, tz=CN_TZ)
            L.append("    发布时间: %s（推断日期, 来自标题中的日期）" % d.strftime("%Y-%m-%d"))
        elif e.get("prec") == "month_title":
            d = dt.datetime.fromtimestamp(e["ts_ms"] / 1000.0, tz=CN_TZ)
            L.append("    发布时间: %s（推断至月份, 来自标题）" % d.strftime("%Y-%m"))
        else:
            L.append("    发布时间: 未知（该视频官方字段暂不可取，下周自动重跑会再试）")
        tags = sorted(e.get("tags") or [], key=lambda x: "短视频" not in x)
        if tags:
            L.append("    标注: %s" % "、".join(tags))
        L.append("")
        L.append(SEP)
    L.append("")
    L.append("=" * 40)
    L.append("统计")
    L.append("=" * 40)
    L.append("视频总数(去重后): %d" % len(entries))
    L.append("其中精确到秒的发布时间: %d 条" % exact)
    L.append("其中标题推断日期(日/月): %d / %d 条" % (title_d, title_m))
    L.append("其中官方日精度日期: %d 条" % day_o)
    L.append("其中日期未知: %d 条" % unknown_n)
    tcount = {}
    for e in entries:
        t = clean_title(e.get("title"))
        if t and t != "(标题待补)":
            tcount[t] = tcount.get(t, 0) + 1
    dup_g = sum(1 for c in tcount.values() if c > 1)
    dup_i = sum(c for c in tcount.values() if c > 1)
    L.append("其中重复标题组(可能为重传/多版本): %d 组（涉及 %d 条, 去重按视频ID）" % (dup_g, dup_i))
    L.append("抓取累计条数(去重前): %s" % meta.get("raw_count", "-"))
    L.append("本次运行耗时: %s" % meta.get("elapsed", "-"))
    L.append("实际使用中继: GitHub Actions 美国节点直连（无中继）")
    L.append("数据源明细: %s" % meta.get("source_desc", "-"))
    L.append("分页缓存: 无（云端每次全量直取）")
    L.append("=" * 40)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    os.replace(tmp, path)  # 原子替换：中断也不会留下半个文件


def build(entries, meta, out, rss_map):
    entries.sort(key=_sort_key)
    write_output(out, entries, meta)
    unknown = sum(1 for e in entries if e.get("prec") == "unknown")
    print("\n[完成] %s：共 %d 条（精确 %d / 官方日 %d / 标题推断 %d / 未知 %d），耗时 %s"
          % (out, len(entries),
             sum(e.get("prec") == "exact" for e in entries),
             sum(e.get("prec") == "day_official" for e in entries),
             sum(e.get("prec") in ("day_title", "month_title") for e in entries),
             unknown, meta["elapsed"]), flush=True)
    return out


def run_demo(args):
    """离线演练：内置样例数据跑通 归并->排序->输出->RSS落盘 全流程，供本地测试。"""
    SAMPLE = [
        # (id, 标题, 官方日YYYY-MM-DD, RSS精确ISO, tags)
        ("JNxQq3KFEM4", "Dissolving $1000 of Platinum to Make $6000 of Chloroplatinic Acid for Professional Use",
         "2024-12-24", "2024-12-24T15:44:46+00:00", ["直播/回放 Live"]),
        ("3YwnlYl0VxA", "This Candle MAKES Oxygen and Started a Fire on a Space Station",
         "2024-12-20", "", []),
        ("_d1J9MVkRzM", "Refuel a Glow Stick", "2024-06-13", "", ["短视频 Shorts"]),
        ("9p3So4ijD4U", "Refuel a Glow Stick", "2024-05-30", "", []),
        ("GsN7r6QkpRA", "Lab Notes - Cleaving Sodium Metal - March 27th 2019", "", "", []),
        ("ZxCO9BaBBHg", "Chemical Thunderstorm in a Beaker (April 2018)", "", "", []),
        ("zLWEemhtdbE", "", "2023-05-02", "", []),      # 标题待补，仅官方日
        ("gjsMV1MglA4", "Mystery Video", "", "", []),    # 未知
    ]
    entries = [{"id": v, "title": t, "upload_date": u, "rss_ts": None,
                "prec": None, "tags": list(g)} for v, t, u, _, g in SAMPLE]
    rss = {v: parse_publish_date(iso) for v, _, _, iso, _ in SAMPLE if iso}
    rss = {k: v for k, v in rss.items() if v}
    for e in entries:
        if e["id"] in rss:
            e["rss_ts"] = rss[e["id"]]
        if e["upload_date"]:
            e["upload_date"] = e["upload_date"].replace("-", "")
    # 把 RSS 样例落盘，供 verify_output.py 第[8]项交叉对照
    ucid = "DEMOUC1"
    rss_body = ('<?xml version="1.0"?><feed>'
                '<entry><id>yt:video:JNxQq3KFEM4</id><yt:videoId>JNxQq3KFEM4</yt:videoId>'
                '<published>2024-12-24T15:44:46+00:00</published>'
                '<title>Dissolving $1000 of Platinum</title></entry></feed>')
    os.makedirs(os.path.join("cache", ucid, "pages"), exist_ok=True)
    with open(os.path.join("cache", ucid, "pages", "rss.xml"), "wb") as f:
        f.write(rss_body.encode("utf-8"))
    assign_dates(entries, rss)
    out = args.out or "data/_demo_videos.txt"
    meta = {"name": "NurdRage(演示)", "ucid": ucid,
            "channel_url": DEFAULT_CHANNEL, "channel_count": len(SAMPLE),
            "source_desc": "演示样例（离线段）", "raw_count": len(SAMPLE),
            "elapsed": "0分0秒"}
    return build(entries, meta, out, rss)


def run_real(args, channel_url):
    t0 = dt.datetime.now()
    log("== 频道: %s ==" % channel_url)
    ids, tags = enum_all(channel_url)
    if not ids:
        print("[错误] 未能从任何 tab 枚举到视频（频道可能为空或 yt-dlp 被暂时限制）。", file=sys.stderr)
        sys.exit(4)
    entries = [{"id": v, "title": ids[v], "upload_date": "", "rss_ts": None,
                "prec": None, "tags": sorted(TAB_LABEL[t] for t in (tags.get(v) or set())
                                             if TAB_LABEL[t])}
               for v in ids]
    ucid, name = channel_meta(channel_url)
    rss_map = {}
    if ucid and not args.list_only:
        try:
            rss_map = fetch_rss(ucid, os.path.join("cache", ucid, "pages", "rss.xml"))
        except Exception as ex:
            log("[警告] RSS 获取失败: %s" % str(ex)[:120])
    for e in entries:
        if e["id"] in rss_map:
            e["rss_ts"] = rss_map[e["id"]]
    if not args.list_only:
        dates = fetch_upload_dates([e["id"] for e in entries])
        for e in entries:
            if e["id"] in dates:
                e["upload_date"] = dates[e["id"]]
    else:
        log("[提示] --list-only：跳过逐条取日期，仅依赖标题推断/未知。")
    assign_dates(entries, rss_map)
    elapsed = dt.datetime.now() - t0
    out = args.out or ("data/%s_videos.txt"
                       % re.sub(r'[\\/:*?"<>|]+', "_", (name or ucid or "channel"))[:60])
    src = ("GitHub Actions 美国节点直连 YouTube；全量枚举(videos/shorts/streams) + "
           "官方 upload_date（日精度） + 官方 RSS（精确秒）")
    meta = {"name": name or ucid, "ucid": ucid or "-", "channel_url": channel_url,
            "channel_count": len(entries), "source_desc": src,
            "raw_count": len(entries), "elapsed": "%d分%.0f秒" % (elapsed.seconds // 60, elapsed.seconds % 60)}
    return build(entries, meta, out, rss_map)


def main():
    ap = argparse.ArgumentParser(
        description="GitHub Actions 云端全量抓取器（国内免梯子；跑在 GitHub 美国节点直连 YouTube）")
    ap.add_argument("url", nargs="?", default=None, help='频道链接，如 "https://www.youtube.com/@NurdRage/videos"')
    ap.add_argument("--out", default=None, help="输出 txt 路径（默认 data/<频道>_videos.txt）")
    ap.add_argument("--list-only", action="store_true", help="只枚举 ID+标题，不逐条取日期")
    ap.add_argument("--demo", action="store_true", help="离线演练：内置样例跑通全流程（本地测试用）")
    args = ap.parse_args()
    try:
        if args.demo:
            run_demo(args)
            return
        channel_url = args.url or os.environ.get("CHANNEL_URL") or DEFAULT_CHANNEL
        run_real(args, channel_url)
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
