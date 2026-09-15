#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py — GitHub Actions 云端全量抓取器 v2（国内免梯子的"一劳永逸"方案）。

为什么在云端跑？
  本机在中国直连 YouTube 被墙、公共 CORS 中继又时好时坏、Wayback 快照也常被墙。
  本脚本运行在 GitHub Actions 的美国服务器上，直连 YouTube —— 无墙、无中继、无 VPN，
  每周定时全量枚举频道视频（ID + 标题 + 时长 + 发布时间），结果写回仓库，墙内随时可下载。

用法：
  python fetch.py                                        # 抓 channels.txt 里的全部频道
  python fetch.py "https://www.youtube.com/@xxx/videos"  # 额外再抓这个频道
  python fetch.py --list-only                            # 只枚举 ID+标题（最快）
  python fetch.py --demo                                 # 离线演练（本地测试用）

输出：data/<频道>_videos.txt（给人看的清单）+ data/<频道>_dates.txt（机器维护的日期缓存）。
依赖：yt-dlp；其余全部走标准库。

日期来源优先级（绝不伪造精确日期）：
  1) 精确到秒 —— 官方 RSS <published>（最近约 15 条）
  2) 精确到秒 —— 官方 Data API v3 snippet.publishedAt（需配置仓库 Secret: YT_API_KEY，配了就是全量精确）
  3) 精确到秒 —— Piped / Invidious 镜像的 uploadDate / published（官方字段的镜像）
  4) 日精度   —— YouTube 播放接口 playerMicroformatRenderer.publishDate；yt-dlp upload_date
  5) 日/月    —— 标题内嵌完整日期（如 "March 27th 2019"），标注"推断"
  6) 年/月/周/日 —— 频道页相对时间（"3 years ago"），标注粒度（兜底主力）
  7) 未知     —— 以上全拿不到才标未知；历史缓存会逐周把它补齐

云端 IP 特点说明：GitHub 数据中心 IP 常被 YouTube 对"单个视频 watch 页"风控，
但"频道标签页 / 列表接口 / 第三方镜像"通常不拦。因此逐条通道失败时会被批量通道兜住。
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone, timedelta
from html import unescape as html_unescape

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CN_TZ = timezone(timedelta(hours=8))
SEP = "-" * 50
VIDEOID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
MONTH_NAME_RE = (r"(?:January|February|March|April|May|June|July|August|September"
                 r"|October|November|December|[A-Z][a-z]{2})")
AGO_UNITS = {"year": 365.25 * 86400, "month": 30.44 * 86400, "week": 7 * 86400,
             "day": 86400, "hour": 3600, "minute": 60, "second": 1}
DEFAULT_CHANNEL = "https://www.youtube.com/@NurdRage/videos"
CHANNELS_FILE = "channels.txt"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 精度等级：数字越大越可信，多源合并时取最大
PREC_RANK = {"unknown": 0, "approx": 1, "month_title": 2, "day_title": 3,
             "day_official": 4, "exact_api": 5, "exact": 6}
SRC_DESC = {
    "rss": "官方 RSS",
    "data_api": "官方 Data API",
    "piped": "Piped 镜像",
    "invidious": "Invidious 镜像",
    "innertube": "YouTube 播放接口",
    "ytdlp": "yt-dlp 元数据",
    "cache": "历史缓存",
    "title": "标题中的日期",
    "relative": "频道页相对时间",
    "wayback": "Wayback 快照",
}

PIPED_INSTANCES = [
    "pipedapi.kavin.rocks", "api.piped.private.coffee", "pipedapi.adminforge.de",
    "pipedapi.leptons.xyz", "pipedapi.ducks.party", "piped-api.lunar.icu",
    "pipedapi.reallyaweso.me", "pipedapi.drgns.space", "api.piped.projectsegfau.lt",
    "pipedapi.smnz.de", "pipedapi.orangenet.cc", "pipedapi.zeteo.dev",
    "piped-api.privacy.com.de", "pipedapi.astartes.nl", "pipedapi.bpwn.ro",
    "pipedapi.nosebs.ru", "api.piped.yt", "pipedapi.vyper.me",
]
INVIDIOUS_INSTANCES = [
    "inv.nadeko.net", "invidious.nerdvpn.de", "yewtu.be", "invidious.f5.si",
    "inv.tux.pizza", "invidious.privacyredirect.com", "iv.melmac.space",
    "invidious.jing.rocks", "invidious.reallyaweso.me", "invidious.dhusch.de",
    "invidious.perennialte.ch", "iv.datura.network", "invidious.materialio.us",
    "invidious.protokolla.fi", "iv.ggtyler.dev", "invidious.einfachzocken.eu",
]
# 播放接口候选客户端（按"云端可用概率"排序，先拿样本试，命中哪个就用哪个）
PLAYER_CLIENTS = [
    ("ANDROID_VR", "1.60.19", {"androidSdkVersion": 32}),
    ("ANDROID", "19.09.37", {"androidSdkVersion": 30}),
    ("IOS", "19.09.3", {"deviceMake": "Apple", "deviceModel": "iPhone16,2",
                        "osName": "iPhone", "osVersion": "17.5.1.21F90"}),
    ("TVHTML5", "7.20240304.10.00", {}),
    ("WEB_EMBEDDED_PLAYER", "1.20240303.00.00", {}),
    ("MWEB", "2.20240304.08.00", {}),
    ("WEB", "2.20250310.00.00", {}),
]
# InnerTube browse 翻页候选客户端
WEB_CLIENTS = [("WEB", None), ("WEB", "2.20250310.00.00"), ("ANDROID", "19.09.37")]
DEFAULT_ITV_VERSION = "2.20250310.00.00"
TAB_LABEL = {"videos": None, "shorts": "短视频 Shorts", "streams": "直播/回放 Live"}


def log(*a):
    print(*a, flush=True)


class Budget:
    """时间预算：Actions 单作业有硬上限，宁可降级也不能被强杀。
    带 parent 时是子预算，取二者的较小剩余（防止单个频道吃光全局预算）。"""

    def __init__(self, seconds, parent=None):
        self.limit = max(60, seconds)
        self.t0 = time.time()
        self.parent = parent

    def left(self):
        here = max(0.0, self.limit - (time.time() - self.t0))
        return min(here, self.parent.left()) if self.parent is not None else here

    def ok(self, need=0.0):
        return self.left() > need


def yt_run(args, timeout=3600):
    """跑一条 yt-dlp 命令，返回 stdout。URL 级错误被 --ignore-errors 吸收。"""
    cmd = ["yt-dlp", "--no-warnings", "--ignore-errors", "--no-cache-dir",
           "--skip-download"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("[yt-dlp] 超时（%ds）" % timeout)
        return ""
    if p.returncode not in (0, 1):
        log("[yt-dlp] 返回码 %d: %s" % (p.returncode, (p.stderr or p.stdout)[-300:]))
    return p.stdout or ""


def http_get_bytes(url, timeout=40, data=None, headers=None):
    """标准库直取（云端直连）。成功返回 bytes，失败返回 None。"""
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, data=data, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as ex:
        log("[网络] 失败 %s: %s" % (url[:70], str(ex)[:80]))
        return None


def http_get_json(url, timeout=30, data=None, headers=None):
    b = http_get_bytes(url, timeout=timeout, data=data, headers=headers)
    if not b:
        return None, "网络失败"
    try:
        return json.loads(b.decode("utf-8", "replace")), None
    except Exception as ex:
        return None, "非 JSON: %s" % str(ex)[:50]


def ensure_base(channel_url):
    for suf in ("/videos", "/shorts", "/streams", "/featured", "/playlists"):
        channel_url = channel_url.split(suf)[0]
    return channel_url.rstrip("/")


def safe_filename(name):
    """频道名 -> 安全文件名。HTML 实体先反转义（曾是 Explosions&amp;Fire 的 bug），
    再去掉文件系统非法字符。"""
    n = html_unescape((name or "").strip())
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", n)
    return n.strip().rstrip(". ")[:60] or "channel"


# ------------------------------------------------ 时间/标题工具

def parse_publish_date(s):
    """ISO8601 -> epoch ms；失败返回 None"""
    try:
        d = dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except Exception:
        return None


def parse_ago_unit(text):
    """'3 years ago' -> (秒数, 精度单位)；失败返回 (None, None)"""
    m = re.search(r"(\d+)\s+(year|month|week|day|hour|minute|second)s?\s+ago",
                  (text or "").lower())
    if not m:
        return (None, None)
    return (int(m.group(1)) * AGO_UNITS[m.group(2)], m.group(2))


def clean_title(t):
    """清理标题残留（HTML 实体、" - YouTube" 后缀、多余空白）"""
    s = html_unescape((t or "").strip())
    if not s:
        return ""
    s = re.sub(r"(?:\s*[-–—]\s*)?YouTube$", "", s).strip()
    return re.sub(r"\s+", " ", s)


def parse_title_date(title):
    """从标题解析完整日期 -> (epoch_ms, 'day_title'|'month_title') 或 None。
    只在标题含完整年份时推断（没有年份的月+日不假装精确）。"""
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


def format_approx(ms, prec):
    """按真实精度渲染相对文本推断日期：只显示能保证的粒度，绝不伪造精确日期。"""
    d = dt.datetime.fromtimestamp(ms / 1000.0, tz=CN_TZ)
    if prec == "month":
        return '    发布时间: %s（推断至月份, ±1个月, 页面相对文本）' % d.strftime("%Y-%m")
    if prec == "week":
        return '    发布时间: %s（推断日期, ±1周, 页面相对文本）' % d.strftime("%Y-%m-%d")
    if prec == "day":
        return '    发布时间: %s（推断日期, 页面相对文本）' % d.strftime("%Y-%m-%d")
    if prec == "year":
        return '    发布时间: %d年（推断年份, ±1年, 页面相对文本）' % d.year
    return '    发布时间: %s（推断日期, 精度未知, 页面相对文本）' % d.strftime("%Y-%m-%d")


def fmt_duration(secs):
    """秒 -> '1:24:32 (1h24m32s)' 或 '5:32 (5m32s)'；拿不到返回 '未知'。"""
    if not secs or secs <= 0:
        return "未知"
    s = int(secs)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d (%dh%dm%ds)" % (h, m, sec, h, m, sec)
    return "%d:%02d (%dm%ds)" % (m, sec, m, sec)


def downgrade_if_midnight(ms):
    """有些镜像把发布时间抹成当天 0 点。秒数恰为整日 -> 只敢声称日精度。"""
    if ms % 86400000 == 0:
        d = dt.datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return (int(d.replace(hour=0, minute=0, second=0).timestamp() * 1000), "day_official")
    return (ms, "exact_api")


# ------------------------------------------------ 枚举 / 频道信息

def enum_all(channel_url, deadline=None):
    """对 videos/shorts/streams 三个 tab 各抓一次取并集。
    返回 (ids:{id:title}, tags:{id:set(tab)}, durations:{id:秒})。
    时长与 id/标题取自同一条 yt-dlp 条目（同一行），自洽不错位。"""
    base = ensure_base(channel_url)
    ids, tags, durations = {}, {}, {}

    def tmo(cap=2400):
        return cap if deadline is None else int(max(60, min(cap, deadline.left())))

    def absorb(out, tab=None):
        got = 0
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            vid = parts[0].strip()
            if not VIDEOID_RE.fullmatch(vid):
                continue
            t = clean_title(parts[1])
            if t and not ids.get(vid):
                ids[vid] = t
            if tab:
                tags.setdefault(vid, set()).add(tab)
            dur = (parts[2] if len(parts) > 2 else "").strip()
            if re.fullmatch(r"\d+", dur):
                durations[vid] = int(dur)
            got += 1
        return got

    for tab in ("videos", "shorts", "streams"):
        if deadline is not None and not deadline.ok(30):
            log("[枚举] 时间预算不足，跳过 tab=%s" % tab)
            continue
        url = "%s/%s" % (base, tab)
        out = yt_run(["--flat-playlist", "--print",
                      "%(id)s\t%(title)s\t%(duration)s", url], timeout=tmo())
        log("[枚举] tab=%s 获得 %d 条（累计 %d）" % (tab, absorb(out, tab), len(ids)))
    if not ids:  # 兜底：直接用调用者给的 URL 抽取一次
        out = yt_run(["--flat-playlist", "--print",
                      "%(id)s\t%(title)s\t%(duration)s", base], timeout=tmo())
        absorb(out)
    return ids, tags, durations


def channel_meta(channel_url):
    """从频道 /videos 页解析 (ucid, name, html)。HTML 失败时退化为 yt-dlp 枚举结果。"""
    base = ensure_base(channel_url)
    ucid, name = None, None
    html = http_get_bytes(base + "/videos")
    if html:
        m = (re.search(rb'"channelId":"(UC[A-Za-z0-9_-]{22})"', html)
             or re.search(rb'"externalId":"(UC[A-Za-z0-9_-]{22})"', html)
             or re.search(rb'"browseId":"(UC[A-Za-z0-9_-]{22})"', html))
        if m:
            ucid = m.group(1).decode()
        t = re.search(rb"<title>([^<]*)</title>", html)
        if t:
            name = clean_title(t.group(1).decode("utf-8", "replace")
                               .replace(" - YouTube", "").strip())
        if not name:
            m2 = re.search(rb'"title":"([^"]{1,200})","navigationEndpoint"', html)
            if m2:
                name = clean_title(m2.group(1).decode("utf-8", "replace"))
    if (not ucid or ucid == "NA") or not name:
        out = yt_run(["--flat-playlist", "--print", "%(channel_id)s\t%(channel)s",
                      "--playlist-items", "1", base], timeout=600)
        for line in out.splitlines():
            if "\t" not in line:
                continue
            a, b = line.split("\t", 1)
            if (not ucid or ucid == "NA") and a.strip() and len(a.strip()) == 24:
                ucid = a.strip()
            if not name and b.strip() and b.strip() != "NA":
                name = clean_title(b.strip())
    return ucid, name, html


# ------------------------------------------------ 日期数据源

def dates_from_rss(ucid, dst):
    """官方 RSS：最近约 15 条，精确到秒。返回 (id -> (ms,'exact','rss'), id -> 标题)。
    原始 XML 落盘供 verify_output.py 交叉对照。"""
    out, titles = {}, {}
    if not ucid:
        return out, titles
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + ucid
    for attempt in (1, 2):
        body = http_get_bytes(url)
        if not body:
            if attempt == 2:
                log("[RSS] 获取失败（频道 ID：%s）" % ucid)
            else:
                time.sleep(1)
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(body)
        except Exception:
            pass
        out, titles = {}, {}
        for vid, pub, title in re.findall(
                rb"<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)</published>"
                rb".*?<title>([^<]+)</title>", body, re.S):
            vid = vid.decode("utf-8", "replace")
            ts = parse_publish_date(pub.decode("utf-8", "replace"))
            if ts:
                out[vid] = (ts, "exact", "rss")
            t = clean_title(title.decode("utf-8", "replace"))
            if t:
                titles[vid] = t
        if len(out) >= 8:
            break
        if attempt == 1:
            log("[RSS] 仅有 %d 条，稍候重试一次" % len(out))
            time.sleep(1)
    log("[RSS] 获得 %d 条精确日期" % len(out))
    return out, titles


def dates_from_data_api(ids, api_key, deadline):
    """官方 YouTube Data API v3（需仓库 Secret: YT_API_KEY）。
    一次请求最多 50 个视频、1 个配额单位；10000 单位/天的免费额度足够上千个视频。
    配了它就是"全部视频精确到秒"的质量上限档。返回 id -> (ms,'exact','data_api')。"""
    res = {}
    if not api_key or not ids:
        return res
    idlist = list(ids)
    for i in range(0, len(idlist), 50):
        if not deadline.ok(15):
            log("[DataAPI] 预算不足，剩余 %d 条未取" % (len(idlist) - i))
            break
        url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails"
               "&maxResults=50&id=%s&key=%s"
               % (",".join(idlist[i:i + 50]), urllib.parse.quote(api_key)))
        d, err = http_get_json(url, timeout=30)
        if err:
            log("[DataAPI] 第 %d 批失败：%s" % (i // 50 + 1, err))
            break
        if "error" in (d or {}):
            log("[DataAPI] 接口报错：%s" % str(d["error"])[:200])
            break
        for it in (d or {}).get("items") or []:
            vid = it.get("id")
            ts = parse_publish_date((it.get("snippet") or {}).get("publishedAt"))
            if vid and VIDEOID_RE.fullmatch(vid) and ts:
                res[vid] = (ts, "exact", "data_api")
        log("[DataAPI] %d/%d（命中 %d）" % (min(i + 50, len(idlist)), len(idlist), len(res)))
    return res


def _take_mirror(res, vid, ms, src):
    if vid and VIDEOID_RE.fullmatch(str(vid)) and isinstance(ms, (int, float)) and ms > 0:
        prec_ms, prec = downgrade_if_midnight(int(ms))
        res[str(vid)] = (prec_ms, prec, src)


def dates_from_piped(ucid, want, deadline, state):
    """Piped 镜像：/channel/<ucid> 翻页，每条带 uploadDate（官方发布时间毫秒）。
    翻完整个频道或覆盖率过半就认这个实例，否则换下一个实例（保留已有结果）。"""
    res = {}
    if not ucid:
        return res
    hosts = ([state["piped"]] if state.get("piped") else []) + \
        [h for h in PIPED_INSTANCES if h != state.get("piped")]
    for host in hosts:
        if not deadline.ok(20):
            log("[Piped] 时间预算不足，停止")
            break
        url = "https://%s/channel/%s" % (host, ucid)
        pages, complete = 0, False
        while url and pages < 40 and deadline.ok(10):
            d, err = http_get_json(url, timeout=25)
            if err:
                if pages == 0:
                    log("[Piped] %s 不可用：%s" % (host, err))
                break
            got = 0
            for s in (d or {}).get("relatedStreams") or []:
                if not isinstance(s, dict):
                    continue
                mm = re.search(r"v=([A-Za-z0-9_-]{11})", s.get("url") or "")
                before = len(res)
                _take_mirror(res, s.get("videoId") or (mm.group(1) if mm else None),
                             s.get("uploadDate"), "piped")
                if len(res) > before:
                    got += 1
            pages += 1
            nxt = (d or {}).get("nextpage")
            complete = not nxt          # 没有下一页 = 这个实例已把整个频道翻完
            log("[Piped] %s 第 %d 页：+%d 条（累计 %d/%d）"
                % (host, pages, got, len(res), len(want)))
            if complete or (want and all(k in res for k in want)):
                break
            url = ("https://%s/nextpage/channel/%s?nextpage=%s"
                   % (host, ucid, urllib.parse.quote(nxt, safe="")))
            time.sleep(0.2)
        if complete or len(res) >= 0.5 * max(1, len(want)):
            state["piped"] = host
            break
    return res


def dates_from_invidious(ucid, want, deadline, state):
    """Invidious 镜像：/api/v1/channels/<ucid>/videos 翻页，每条带 published（Unix 秒）。"""
    res = {}
    if not ucid:
        return res
    hosts = ([state["inv"]] if state.get("inv") else []) + \
        [h for h in INVIDIOUS_INSTANCES if h != state.get("inv")]
    for host in hosts:
        if not deadline.ok(20):
            log("[Invidious] 时间预算不足，停止")
            break
        complete, dry = False, 0
        for page in range(1, 41):
            if not deadline.ok(10):
                break
            d, err = http_get_json("https://%s/api/v1/channels/%s/videos?page=%d"
                                   % (host, ucid, page), timeout=25)
            if err:
                if page == 1:
                    log("[Invidious] %s 不可用：%s" % (host, err))
                complete = page > 1
                break
            if not isinstance(d, list) or not d:
                complete = page > 1      # 该实例已无更多页
                break
            got = 0
            for s in d:
                if not isinstance(s, dict):
                    continue
                before = len(res)
                _take_mirror(res, s.get("videoId"), (s.get("published") or 0) * 1000,
                             "invidious")
                if len(res) > before:
                    got += 1
            log("[Invidious] %s 第 %d 页：+%d 条（累计 %d/%d）"
                % (host, page, got, len(res), len(want)))
            if want and all(k in res for k in want):
                complete = True
                break
            # 连续两页零新增 -> 该实例在重复吐同一页，别死循环
            dry = dry + 1 if got == 0 else 0
            if dry >= 2:
                complete = True
                break
            time.sleep(0.2)
        if complete or len(res) >= 0.5 * max(1, len(want)):
            state["inv"] = host
            break
    return res


def date_from_mirror_video(vid, deadline, state):
    """单条视频的镜像补日期（只给少量残尾用，避免大量请求）。
    返回 (ms, prec) 或 None。"""
    for host in ([state["inv"]] if state.get("inv") else []) + INVIDIOUS_INSTANCES[:6]:
        if not deadline.ok(8):
            return None
        d, err = http_get_json("https://%s/api/v1/videos/%s" % (host, vid), timeout=20)
        if err or not isinstance(d, dict):
            continue
        ts = parse_publish_date(d.get("published") if isinstance(d.get("published"), str) else "")
        if not ts and isinstance(d.get("published"), (int, float)):
            ts = int(d["published"]) * 1000
        if ts:
            state["inv"] = host
            return downgrade_if_midnight(ts)
    for host in ([state["piped"]] if state.get("piped") else []) + PIPED_INSTANCES[:6]:
        if not deadline.ok(8):
            return None
        d, err = http_get_json("https://%s/streams/%s" % (host, vid), timeout=20)
        if err or not isinstance(d, dict):
            return None
        ts = parse_publish_date(d.get("uploadDate") if isinstance(d.get("uploadDate"), str) else "")
        if ts:
            state["piped"] = host
            return downgrade_if_midnight(ts)
    return None


# --- 频道页相对时间（云 IP 被 watch 页风控时的兜底主力） ---

def extract_rel_from_data(o, anchor_ms, rel=None, tokens=None, titles=None):
    """遍历任意频道/续拉 JSON：提取 (videoId -> (相对毫秒, 精度))、翻页 token，
    以及**同一 renderer 内的标题**（id↔标题取自同一对象，自洽不错位）。
    兼容 lockupViewModel / videoRenderer / reelItemRenderer / publishedTimeText 各布局。"""
    if rel is None:
        rel = {}
    if tokens is None:
        tokens = []
    if titles is None:
        titles = {}
    if isinstance(o, dict):
        cont = o.get("continuationCommand")
        if isinstance(cont, dict) and isinstance(cont.get("token"), str):
            tokens.append(cont["token"])
        vid, ago, ttl = None, None, None
        lv = o.get("lockupViewModel")
        if isinstance(lv, dict):
            vid = lv.get("contentId")
            md = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
            ttl = (md.get("title", {}) or {}).get("content", "")
            rows = (md.get("metadata", {}).get("contentMetadataViewModel", {})
                    or {}).get("metadataRows", []) or []
            for row in rows:
                for p in row.get("metadataParts", []) or []:
                    t = (p.get("text", {}) or {}).get("content", "")
                    if "ago" in t.lower():
                        ago = t
        elif "videoId" in o or "publishedTimeText" in o:
            vid = o.get("videoId") or o.get("contentId")
            pt = o.get("publishedTimeText") or {}
            ago = (pt.get("simpleText") if isinstance(pt, dict) else None) \
                or o.get("publishedTimeText")
            tr = o.get("title")
            if isinstance(tr, dict):
                runs = tr.get("runs")
                if isinstance(runs, list):
                    ttl = "".join((r.get("text", "") or "") for r in runs)
                elif tr.get("simpleText"):
                    ttl = tr.get("simpleText")
        if vid and VIDEOID_RE.fullmatch(str(vid)):
            if ttl:
                titles[str(vid)] = str(ttl)
            if ago:
                secs, prec = parse_ago_unit(str(ago))
                if secs:
                    rel[str(vid)] = (int(anchor_ms - secs * 1000), prec)
        for v in o.values():
            extract_rel_from_data(v, anchor_ms, rel, tokens, titles)
    elif isinstance(o, list):
        for v in o:
            extract_rel_from_data(v, anchor_ms, rel, tokens, titles)
    return rel, tokens, titles


def extract_rel_from_html_data(html, anchor_ms):
    """从频道标签页 ytInitialData 提取 (rel, tokens, titles)。处理 \\x22 双转义。"""
    rel, tokens, titles = {}, [], {}
    html = re.sub(rb"\\x22", b'"', html)  # 移动版页面会把 JSON 引号双转义成 \x22
    m = (re.search(rb"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
         or re.search(rb"ytInitialData\s*=\s*(\{.*?\});", html, re.S))
    if not m:
        return rel, tokens, titles
    try:
        data = json.loads(m.group(1).decode("utf-8", "replace"))
    except Exception:
        return rel, tokens, titles
    return extract_rel_from_data(data, anchor_ms, rel, tokens, titles)


def extract_rel_from_html(html, anchor_ms):
    """兼容旧布局：返回 {id: (ms, 精度)}，含邻近匹配兜底。"""
    rel, _, _ = extract_rel_from_html_data(html, anchor_ms)
    if not rel:
        for mm in re.finditer(rb'"videoId":"([A-Za-z0-9_-]{11})"', html):
            window = html[max(0, mm.start() - 2500):mm.start() + 2500]
            agos = re.findall(rb'(\d+\s+(?:year|month|week|day|hour|minute|second)s?\s+ago)',
                              window)
            if agos:
                secs, prec = parse_ago_unit(agos[0].decode())
                if secs:
                    rel[mm.group(1).decode()] = (int(anchor_ms - secs * 1000), prec)
    return rel


def innertube_keys(html):
    """从频道页 HTML 取 (INNERTUBE_API_KEY, 页面客户端版本)。"""
    key, ver = "", ""
    if not html:
        return key, ver
    mk = re.search(rb'"INNERTUBE_API_KEY":"([^"]+)"', html)
    if mk:
        key = mk.group(1).decode()
    mv = re.search(rb'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    if mv:
        ver = mv.group(1).decode()
    return key, ver


def post_browse(token, client_name, client_version, api_key):
    """InnerTube browse 接口 POST 翻页续拉。返回 (data|None, 错误文本|None)。"""
    url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
    if api_key:
        url += "&key=" + urllib.parse.quote(api_key)
    client = {"clientName": client_name, "clientVersion": client_version,
              "hl": "en", "gl": "US"}
    if client_name == "ANDROID":
        client["androidSdkVersion"] = 30
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if client_name == "WEB":
        headers["X-Youtube-Client-Name"] = "1"
        headers["X-Youtube-Client-Version"] = client_version
    req = urllib.request.Request(
        url, data=json.dumps({"context": {"client": client}, "continuation": token})
        .encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except Exception as ex:
        return None, str(ex)[:120]


def _resp_diag(data):
    """从 InnerTube 响应抽诊断要点，便于一眼看出是 token 问题还是被拒。"""
    if not isinstance(data, dict):
        return "非JSON对象"
    keys = ",".join(list(data.keys())[:8])
    err = data.get("error")
    if isinstance(err, dict):
        return "ERROR(%s:%s) keys=[%s]" % (err.get("code"),
                                           str(err.get("message"))[:90], keys)
    try:
        n = len(data.get("onResponseReceivedActions", []) or [])
    except Exception:
        n = -1
    return "keys=[%s] onResponseReceivedActions=%d" % (keys, n)


def fetch_inner_relative_dates(channel_url, anchor_ms, deadline, max_pages=60):
    """兜底主力：三 tab 页面 + InnerTube 翻页续拉，给尽量多视频标上相对时间。
    返回 (rel, titles, api_key, page_ver)。"""
    base = ensure_base(channel_url)
    rel, titles = {}, {}
    key_out, ver_out = "", ""
    for tab in ("videos", "shorts", "streams"):
        if not deadline.ok(30):
            log("[相对时间] 预算不足，跳过 tab=%s" % tab)
            continue
        html = http_get_bytes("%s/%s" % (base, tab))
        if not html:
            continue
        key, page_ver = innertube_keys(html)
        if key and not key_out:
            key_out = key
        if page_ver and not ver_out:
            ver_out = page_ver
        rel0, tokens, titles0 = extract_rel_from_html_data(html, anchor_ms)
        for k, v in rel0.items():
            rel.setdefault(k, v)
        for k, v in titles0.items():
            titles.setdefault(k, v)
        candidates = list(dict.fromkeys(tokens))[:6]
        queue, used = list(candidates), set()
        pages, diags = 0, []
        while queue and pages < max_pages and deadline.ok(15):
            token = queue.pop(0)
            if token in used:
                continue
            used.add(token)
            for cname, cver in WEB_CLIENTS:
                cver = cver or page_ver or DEFAULT_ITV_VERSION
                data, err = post_browse(token, cname, cver, key)
                if data is None:
                    diags.append("[%s/%s] 网络错误: %s" % (cname, cver, err))
                    continue
                relp, tokensp, titlesp = extract_rel_from_data(data, anchor_ms, {}, [], {})
                nxt = tokensp[-1] if tokensp else None
                if relp or nxt:
                    for k, v in relp.items():
                        rel.setdefault(k, v)
                    for k, v in titlesp.items():
                        titles.setdefault(k, v)
                    if nxt and nxt not in used:
                        queue.append(nxt)
                    diags.append("[%s/%s] 新条目%d, 下页token:%s"
                                 % (cname, cver, len(relp), "有" if nxt else "无"))
                    pages += 1
                    break
                diags.append("[%s/%s] %s" % (cname, cver, _resp_diag(data)))
            time.sleep(0.3)
        log("[相对时间] tab=%s 首屏%d条 + 翻页%d页, 候选token%d个, 并集%d"
            % (tab, len(rel0), pages, len(candidates), len(rel)))
        if not pages:
            for d in diags[:5]:
                log("[相对时间]    诊断: %s" % d)
    return rel, titles, key_out, ver_out


# --- 播放接口：逐条要官方 publishDate（日精度），给"批量通道没覆盖到"的缺口收尾 ---

def _player_probe(vid, client, api_key):
    """对单个视频发一次 player 请求，返回 (YYYY-MM-DD|None, playabilityStatus)。"""
    name, ver, extra = client
    url = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
    if api_key:
        url += "&key=" + urllib.parse.quote(api_key)
    c = {"clientName": name, "clientVersion": ver, "hl": "en", "gl": "US"}
    c.update(extra)
    b = http_get_bytes(url, timeout=20,
                       data=json.dumps({"context": {"client": c}, "videoId": vid,
                                        "contentCheckOk": True,
                                        "racyCheckOk": True}).encode("utf-8"),
                       headers={"Content-Type": "application/json"})
    if not b:
        return None, "网络失败"
    try:
        d = json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return None, "非JSON"
    st = ((d.get("playabilityStatus") or {}).get("status")) or "?"
    mm = (d.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    for k in ("publishDate", "uploadDate"):
        v = mm.get(k)
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return v, st
    return None, st


def dates_from_player(ids, api_key, deadline, state):
    """先用少量样本挑出当前云端 IP 下真正能用的客户端，再用它并行补齐所有缺口。
    返回 id -> (ms, 'day_official', 'innertube')。"""
    res = {}
    ids = [v for v in ids if VIDEOID_RE.fullmatch(v)]
    if not ids:
        return res
    sample = ids[:6]
    chosen, report = None, []
    for client in PLAYER_CLIENTS:
        hit = 0
        for vid in sample:
            if not deadline.ok(8):
                break
            d, _ = _player_probe(vid, client, api_key)
            if d:
                hit += 1
        report.append("%s=%d/%d" % (client[0], hit, len(sample)))
        log("[播放接口] 客户端 %s 命中 %d/%d" % (client[0], hit, len(sample)))
        if hit:
            chosen = client
            break
    state["player_probe"] = "; ".join(report)
    if not chosen:
        log("[播放接口] 所有客户端均无日期（云端 IP 被风控），改由镜像/相对时间兜底")
        return res

    def one(vid):
        return vid, (_player_probe(vid, chosen, api_key)[0] if deadline.ok(5) else None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for vid, d in ex.map(one, ids):
            if d:
                y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
                res[vid] = (int(dt.datetime(y, m, dd, tzinfo=timezone.utc)
                                .timestamp() * 1000), "day_official", "innertube")
    log("[播放接口] %s 补齐 %d/%d 条官方日" % (chosen[0], len(res), len(ids)))
    return res


def dates_from_ytdlp_clients(ids, deadline):
    """备用：yt-dlp 换 player_client 逐个取 upload_date（慢，只在缺口大时用）。"""
    res = {}
    ids = [v for v in ids if VIDEOID_RE.fullmatch(v)]
    if not ids:
        return res
    for client in ("android_vr", "ios", "tv", "mweb"):
        if not deadline.ok(30):
            break
        urls = ["https://www.youtube.com/watch?v=%s" % v for v in ids[:40]]
        out = yt_run(["--no-playlist", "--print", "%(id)s\t%(upload_date)s",
                      "--extractor-args", "youtube:player_client=" + client,
                      "--retries", "2"] + urls, timeout=int(max(90, deadline.left())))
        got = 0
        for line in out.splitlines():
            if "\t" not in line:
                continue
            vid, d = line.split("\t", 1)
            d = d.strip()
            if VIDEOID_RE.fullmatch(vid.strip()) and re.fullmatch(r"\d{8}", d):
                y, m, dd = int(d[:4]), int(d[4:6]), int(d[6:8])
                res[vid.strip()] = (int(dt.datetime(y, m, dd, tzinfo=timezone.utc)
                                        .timestamp() * 1000), "day_official", "ytdlp")
                got += 1
        log("[yt-dlp] player_client=%s 命中 %d 条" % (client, got))
        if len(res) >= 0.6 * len(ids):
            break
    return res


def _parse_wb_ts(ts):
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return int(dt.datetime.strptime(ts[:len(fmt)], fmt)
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            continue
    return None


def fetch_wayback_relative(ucid, handle, anchor_ms, deadline, max_snapshots=24):
    """Wayback 快照并集：用各快照当日的相对时间还原当时可见的视频（治残尾）。"""
    cand_urls = []
    if ucid:
        cand_urls.append("https://www.youtube.com/channel/%s/videos" % ucid)
    if handle:
        cand_urls.append("https://www.youtube.com/@%s/videos" % handle)
    snaps = []
    for u in cand_urls:
        if not deadline.ok(30):
            break
        cdx = ("http://web.archive.org/cdx/search/cdx?url=%s&output=json"
               "&fl=timestamp,statuscode&filter=statuscode:200&collapse=timestamp:6&limit=80"
               % urllib.parse.quote(u, safe=""))
        body = http_get_bytes(cdx, timeout=40)
        if not body:
            continue
        try:
            rows = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            continue
        if len(rows) > 1:
            snaps += [(str(r[0]), u) for r in rows[1:] if r and str(r[0]).isdigit()]
            log("[Wayback] %s 命中 %d 个快照" % (u, len(rows) - 1))
    snaps = sorted(set(snaps))
    if len(snaps) > max_snapshots:      # 时间轴均匀抽样，控制耗时
        step = len(snaps) / max_snapshots
        snaps = [snaps[int(i * step)] for i in range(max_snapshots)]
    log("[Wayback] 将抓取 %d 个快照" % len(snaps))
    rel = {}
    for i, (ts, u) in enumerate(snaps, 1):
        if not deadline.ok(20):
            log("[Wayback] 预算不足，中断于 %d/%d" % (i, len(snaps)))
            break
        html = http_get_bytes("https://web.archive.org/web/%sid_/%s" % (ts, u), timeout=40)
        if not html:
            continue
        for k, v in extract_rel_from_html(html, _parse_wb_ts(ts) or anchor_ms).items():
            rel.setdefault(k, v)
        if i % 6 == 0 or i == len(snaps):
            log("[Wayback] 快照 %d/%d（并集 %d）" % (i, len(snaps), len(rel)))
    return rel


# ------------------------------------------------ 历史日期缓存（逐周收敛的关键）

def cache_path_for(out_path):
    return (re.sub(r"_videos\.txt$", "_dates.txt", out_path)
            if out_path.endswith("_videos.txt") else out_path + ".dates.txt")


def load_date_cache(path):
    """读上一轮留下的日期缓存：id -> (ms, prec, src)。"""
    res = {}
    if not os.path.isfile(path):
        return res
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "\t" not in line:
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) < 4:
                    continue
                vid, prec = p[0].strip(), p[2].strip()
                if not VIDEOID_RE.fullmatch(vid) or prec not in PREC_RANK:
                    continue
                try:
                    ms = int(p[1].strip())
                except ValueError:
                    continue
                if ms > 0:
                    res[vid] = (ms, prec, p[3].strip())
    except Exception as ex:
        log("[缓存] 读取失败：%s" % str(ex)[:80])
    return res


def save_date_cache(path, merged, min_rank=None):
    """只把"官方级"结论写回缓存（RSS/官方接口/镜像/播放接口/yt-dlp），
    标题推断与模糊相对时间不回写——那些每周都能就地重算，且标题可能被改。"""
    min_rank = PREC_RANK["day_official"] if min_rank is None else min_rank
    keep = {k: v for k, v in merged.items()
            if v and v[0] and PREC_RANK.get(v[1], 0) >= min_rank}
    now = dt.datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = ["# 日期缓存（由 fetch.py 自动维护，请勿手动编辑）",
             "# video_id\tepoch_ms\tprecision\tsource\t最近确认时间(UTC)"]
    for vid in sorted(keep):
        ms, prec, src = keep[vid]
        lines.append("%s\t%d\t%s\t%s\t%s" % (vid, ms, prec, src, now))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return len(keep)


def merge_dates(entries, sources):
    """把各来源的日期归并进条目：同一视频取可信度最高的那个。
    sources 是有序列表 [(名称, {id: (ms, prec, src)})]，同精度时先出现的优先。"""
    stats = {}
    for name, mapping in sources:
        n = 0
        for e in entries:
            new = mapping.get(e["id"])
            if not new:
                continue
            cur = e.get("_date")
            if cur is None or PREC_RANK.get(new[1], 0) > PREC_RANK.get(cur[1], 0):
                e["_date"] = new
                n += 1
        stats[name] = n
    for e in entries:
        d = e.get("_date")
        if d:
            e["ts_ms"], e["prec"], e["src"] = d[0], d[1], d[2]
    return stats


def apply_title_and_relative(entries, rel):
    """标题日期 > 相对文本推断，两级兜底。"""
    n_title, n_rel = 0, 0
    for e in entries:
        if PREC_RANK.get(e.get("prec") or "", 0) >= PREC_RANK["day_title"]:
            continue
        pair = parse_title_date(e.get("title") or "")
        if pair:
            e["ts_ms"], e["prec"], e["src"] = pair[0], pair[1], "title"
            n_title += 1
            continue
        if e["id"] in rel:
            ms, prec = rel[e["id"]]
            e["approx_ms"], e["approx_prec"] = ms, prec
            e["prec"], e["src"] = "approx", "relative"
            n_rel += 1
        elif not e.get("prec"):
            e["prec"], e["src"] = "unknown", "unknown"
    log("[兜底] 标题推断 %d 条，页面相对文本 %d 条" % (n_title, n_rel))
    return n_title, n_rel


def _sort_key(e):
    ts = e.get("ts_ms") or e.get("approx_ms")
    return (1, 0) if not ts else (0, -ts)


# ------------------------------------------------ 输出

def read_previous_ids(path):
    """读上一版清单里的 id -> 标题，用于"本次变化"归档对照。"""
    ids, cur = {}, None
    if not os.path.isfile(path):
        return ids
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"【\d+】视频名称: (.*)", line)
                if m:
                    cur = m.group(1).strip()
                    continue
                s = line.strip()
                if s.startswith("视频链接: ") and cur is not None:
                    mm = re.search(r"watch\?v=([A-Za-z0-9_-]{11})", s)
                    if mm:
                        ids[mm.group(1)] = cur
                    cur = None
    except Exception:
        return {}
    return ids


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
    ch = meta.get("changes") or {}
    if ch:
        L.append("本次变化: 新增 %d 条 / 从频道消失 %d 条（相比上次抓取）"
                 % (len(ch.get("added") or []), len(ch.get("removed") or [])))
    L.append("=" * 40)

    cnt = {}
    for e in entries:
        cnt[e.get("prec") or "unknown"] = cnt.get(e.get("prec") or "unknown", 0) + 1

    for i, e in enumerate(entries, 1):
        L.append("")
        L.append("【%d】视频名称: %s" % (i, clean_title(e.get("title")) or "(标题待补)"))
        L.append("    视频链接: https://www.youtube.com/watch?v=%s" % e["id"])
        L.append("    时长: %s" % fmt_duration(e.get("duration")))
        p = e.get("prec")
        if p == "exact":
            L.append("    发布时间: %s (UTC+8)   [原始: %s UTC]"
                     % (fmt_cn(e["ts_ms"]), fmt_utc(e["ts_ms"])))
        elif p == "exact_api":
            L.append("    发布时间: %s (UTC+8)（精确到秒, 来自%s）"
                     % (fmt_cn(e["ts_ms"]), SRC_DESC.get(e.get("src"), "官方数据接口")))
        elif p == "day_official":
            d = dt.datetime.fromtimestamp(e["ts_ms"] / 1000.0, tz=CN_TZ)
            L.append("    发布时间: %s（日精度, 来自%s）"
                     % (d.strftime("%Y-%m-%d"),
                        SRC_DESC.get(e.get("src"), "YouTube 官方字段")))
        elif p == "day_title":
            d = dt.datetime.fromtimestamp(e["ts_ms"] / 1000.0, tz=CN_TZ)
            L.append("    发布时间: %s（推断日期, 来自标题中的日期）" % d.strftime("%Y-%m-%d"))
        elif p == "month_title":
            d = dt.datetime.fromtimestamp(e["ts_ms"] / 1000.0, tz=CN_TZ)
            L.append("    发布时间: %s（推断至月份, 来自标题）" % d.strftime("%Y-%m"))
        elif p == "approx":
            L.append(format_approx(e["approx_ms"], e.get("approx_prec") or "year"))
        else:
            L.append("    发布时间: 未知（该视频官方字段暂不可取，下周自动重跑会再试）")
        tags = sorted(e.get("tags") or [], key=lambda x: "短视频" not in x)
        if tags:
            L.append("    标注: %s" % "、".join(tags))
        L.append("")
        L.append(SEP)

    exact = cnt.get("exact", 0)
    exact_api = cnt.get("exact_api", 0)
    day_o = cnt.get("day_official", 0)
    t_d = cnt.get("day_title", 0)
    t_m = cnt.get("month_title", 0)
    approx = cnt.get("approx", 0)
    unknown_n = cnt.get("unknown", 0)

    L.append("")
    L.append("=" * 40)
    L.append("统计")
    L.append("=" * 40)
    L.append("视频总数(去重后): %d" % len(entries))
    L.append("其中精确到秒的发布时间: %d 条（官方 RSS %d / 官方接口与镜像 %d）"
             % (exact + exact_api, exact, exact_api))
    L.append("其中官方日精度日期: %d 条" % day_o)
    L.append("其中标题推断日期(日/月): %d / %d 条" % (t_d, t_m))
    L.append("其中相对文本推断(年/月/周/日): %d 条" % approx)
    L.append("其中日期未知: %d 条" % unknown_n)
    tcount = {}
    for e in entries:
        t = clean_title(e.get("title"))
        if t and t != "(标题待补)":
            tcount[t] = tcount.get(t, 0) + 1
    dup_g = sum(1 for c in tcount.values() if c > 1)
    L.append("其中重复标题组(可能为重传/多版本): %d 组（涉及 %d 条, 去重按视频ID）"
             % (dup_g, sum(c for c in tcount.values() if c > 1)))
    L.append("抓取累计条数(去重前): %s" % meta.get("raw_count", "-"))
    L.append("本次运行耗时: %s" % meta.get("elapsed", "-"))
    L.append("实际使用中继: GitHub Actions 美国节点直连（无中继）")
    L.append("数据源明细: %s" % meta.get("source_desc", "-"))

    L.append("")
    L.append("=" * 40)
    L.append("数据通道明细（本次运行，排障用）")
    L.append("=" * 40)
    for k, v in (meta.get("counts") or []):
        L.append("- %s: %s 条" % (k, v) if v is not None else "- %s（本次跳过）" % k)
    if meta.get("prec_by_src"):
        L.append("- 最终各来源条数: %s" % meta["prec_by_src"])
    L.append("- 提示: 想让它每次都能精确到秒，给仓库加一个 Secret 名为 YT_API_KEY"
             "（YouTube Data API v3 的免费 key）即可，脚本会自动启用官方接口通道。")

    if ch and (ch.get("removed") or []):
        L.append("")
        L.append("=" * 40)
        L.append("已从频道消失的视频（本次未再出现，可能是删除/转私享/下架）")
        L.append("=" * 40)
        for vid, title in (ch["removed"] or [])[:50]:
            L.append("- %s  https://www.youtube.com/watch?v=%s" % (title or "(标题未知)", vid))
        if len(ch["removed"]) > 50:
            L.append("- …另有 %d 条" % (len(ch["removed"]) - 50))

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    os.replace(tmp, path)  # 原子替换：中断也不会留下半个文件


def build(entries, meta, out, prev_path=None):
    entries.sort(key=_sort_key)
    if prev_path:
        old = read_previous_ids(prev_path)
        if old:
            new_ids = {e["id"] for e in entries}
            meta["changes"] = {
                "added": [e["id"] for e in entries if e["id"] not in old],
                "removed": [(vid, t) for vid, t in old.items() if vid not in new_ids],
            }
            log("[归档] 相比上次：新增 %d 条，消失 %d 条"
                % (len(meta["changes"]["added"]), len(meta["changes"]["removed"])))
    write_output(out, entries, meta)
    cnt = {}
    for e in entries:
        cnt[e.get("prec") or "unknown"] = cnt.get(e.get("prec") or "unknown", 0) + 1
    solid = cnt.get("exact", 0) + cnt.get("exact_api", 0) + cnt.get("day_official", 0)
    print("\n[完成] %s：共 %d 条（精确 %d / 官方日 %d / 标题推断 %d / 相对文本 %d / 未知 %d），"
          "可靠日期(秒或日) %d 条占 %.0f%%，耗时 %s"
          % (out, len(entries), cnt.get("exact", 0) + cnt.get("exact_api", 0),
             cnt.get("day_official", 0), cnt.get("day_title", 0) + cnt.get("month_title", 0),
             cnt.get("approx", 0), cnt.get("unknown", 0), solid,
             100.0 * solid / max(1, len(entries)), meta.get("elapsed", "-")), flush=True)
    return out


# ------------------------------------------------ 单频道抓取

def run_channel(channel_url, args, deadline):
    t0 = dt.datetime.now()
    log("\n" + "=" * 60)
    log("== 频道: %s ==" % channel_url)

    ids, tags, durations = enum_all(channel_url, deadline)
    if not ids:
        log("[错误] 未能从任何 tab 枚举到视频（频道可能为空或 yt-dlp 被暂时限制）。")
        return None
    ucid, name, html = channel_meta(channel_url)
    log("[频道] 名称=%s ID=%s" % (name or "-", ucid or "-"))

    out = args.out or ("data/%s_videos.txt" % safe_filename(name or "channel"))
    # 旧版把频道名的 HTML 实体原样写进文件名（Explosions&amp;Fire_...），这里兼容对照一次
    prev = out
    if not os.path.isfile(prev) and name:
        alt = os.path.join(os.path.dirname(out),
                           safe_filename(name.replace("&", "&amp;")) + "_videos.txt")
        if os.path.isfile(alt):
            prev = alt
            log("[归档] 沿用上一版文件名做对照: %s" % alt)

    entries = [{"id": v, "title": ids[v], "duration": durations.get(v),
                "tags": sorted(TAB_LABEL[t] for t in (tags.get(v) or set()) if TAB_LABEL[t])}
               for v in ids]
    counts, sources = [], []
    state = {}
    api_key = (os.environ.get("YT_API_KEY") or "").strip()

    # 0) 历史缓存：先把已确认的日期铺上，任何数据源抽风都不会让清单退化
    cache_p = cache_path_for(out)
    cache = load_date_cache(cache_p)
    if cache:
        hit = len([e for e in entries if e["id"] in cache])
        sources.append(("cache", cache))
        counts.append(("历史日期缓存（本轮直接复用）", hit))
        log("[缓存] 命中 %d/%d 条" % (hit, len(entries)))

    # 1) 官方 RSS（精确到秒，最近约 15 条）
    rss_map, rss_titles = dates_from_rss(
        ucid, os.path.join("cache", ucid or "unknown", "pages", "rss.xml"))
    if rss_map:
        sources.append(("rss", rss_map))
    counts.append(("官方 RSS（精确到秒）", len(rss_map)))
    for e in entries:
        # RSS 标题只用于"填空"：RSS 快照可能是发布时的旧标题（视频后改过名），
        # 绝不能覆盖网格/InnerTube 的现行标题（曾因此把顶格视频标题改错）。
        if e["id"] in rss_titles and not (e.get("title") or "").strip():
            e["title"] = rss_titles[e["id"]]

    # 2) 官方 Data API v3（配了 key 就是全量精确到秒，质量上限档）
    if api_key and not args.list_only:
        api = dates_from_data_api(list(ids), api_key, deadline)
        if api:
            sources.append(("data_api", api))
        counts.append(("官方 Data API v3（精确到秒）", len(api)))
    elif not args.list_only:
        counts.append(("官方 Data API v3（未配置 YT_API_KEY）", None))
        log("[DataAPI] 未配置 YT_API_KEY，跳过（配置后本通道可给全部视频精确到秒的日期）")

    # 3)(4) 第三方镜像：一次翻页带回整频道 + 精确时间，是"免 key 全量精确日期"的主力
    if not args.list_only:
        need = set(ids)
        piped = dates_from_piped(ucid, need, deadline, state) if deadline.ok(30) else {}
        sources.append(("piped", piped))
        counts.append(("Piped 镜像（官方时间戳）", len(piped)))
        inv = dates_from_invidious(ucid, need, deadline, state) if deadline.ok(30) else {}
        sources.append(("invidious", inv))
        counts.append(("Invidious 镜像（官方时间戳）", len(inv)))

    # 5) 频道页相对时间（同时拿到 InnerTube key，供第 6 步逐条播放接口用）
    anchor_ms = int(dt.datetime.now(timezone.utc).timestamp() * 1000)
    rel, ititles, itkey, itver = fetch_inner_relative_dates(channel_url, anchor_ms, deadline)
    fixed = 0
    for e in entries:
        # InnerTube 每条 renderer 里自洽的 id↔标题（同一对象取出，不会串位），
        # 覆盖掉网格枚举顶格错配 / RSS 旧标题的问题。
        t = clean_title(ititles.get(e["id"]) or "")
        if t and t != e.get("title"):
            e["title"] = t
            fixed += 1
    if fixed:
        log("[标题] InnerTube 同源修正 %d 条" % fixed)
    counts.append(("InnerTube 频道页相对时间（模糊兜底）", len(rel)))

    if args.with_wayback and not args.list_only:
        try:
            mh = re.search(r"youtube\.com/(?:@|c/|user/)([^/?#]+)", channel_url)
            wb = fetch_wayback_relative(ucid, (mh.group(1).strip() if mh else ""),
                                        anchor_ms, deadline)
            if wb:
                sources.append(("wayback", wb))
            counts.append(("Wayback 快照", len(wb)))
        except Exception as ex:
            log("[Wayback] 失败: %s" % str(ex)[:100])

    merge_dates(entries, sources)
    gaps = [e["id"] for e in entries
            if PREC_RANK.get(e.get("prec") or "", 0) < PREC_RANK["day_official"]]
    log("[缺口] 仍缺官方秒/日精度的视频: %d 条" % len(gaps))

    # 6) 播放接口逐条补官方日（只补缺口；先用样本挑出当前可用的客户端）
    if gaps and not args.list_only and deadline.ok(60):
        try:
            p = dates_from_player(gaps, itkey, deadline, state)
            if p:
                sources.append(("innertube", p))
                merge_dates(entries, sources)
            counts.append(("YouTube 播放接口（日精度）", len(p)))
        except Exception as ex:
            log("[播放接口] 失败: %s" % str(ex)[:120])
    if state.get("player_probe"):
        counts.append(("播放接口客户端探测（命中/样本）", state["player_probe"]))

    # 6b) 单条镜像补漏：只对"连相对时间都没有"的残尾逐条试，量小、快
    tail = [e["id"] for e in entries
            if PREC_RANK.get(e.get("prec") or "", 0) < PREC_RANK["day_official"]
            and e["id"] not in rel]
    if tail and not args.list_only and deadline.ok(60):
        filled, tried = {}, 0
        for vid in tail:
            if not deadline.ok(10) or tried >= 120:
                break
            tried += 1
            got = date_from_mirror_video(vid, deadline, state)
            if got:
                filled[vid] = (got[0], got[1],
                               "invidious" if state.get("inv") else "piped")
            if tried % 20 == 0:
                log("[镜像补漏] 已试 %d 条，命中 %d 条" % (tried, len(filled)))
        if filled:
            sources.append(("mirror_video", filled))
            merge_dates(entries, sources)
        counts.append(("单条镜像补漏（残尾）", len(filled)))

    # 7) yt-dlp 客户端轮换（可选，慢）
    if args.with_upload_dates and not args.list_only:
        g3 = [e["id"] for e in entries
              if PREC_RANK.get(e.get("prec") or "", 0) < PREC_RANK["day_official"]]
        if g3:
            yd = dates_from_ytdlp_clients(g3, deadline)
            if yd:
                sources.append(("ytdlp", yd))
                merge_dates(entries, sources)
            counts.append(("yt-dlp 客户端元数据（日精度）", len(yd)))

    # 8) 标题日期 + 相对文本兜底
    n_t, n_r = apply_title_and_relative(entries, rel)
    counts.append(("标题推断日期", n_t))
    counts.append(("页面相对文本（模糊）", n_r))

    by_src = {}
    for e in entries:
        k = e.get("src") or "unknown"
        by_src[k] = by_src.get(k, 0) + 1
    elapsed = (dt.datetime.now() - t0).seconds
    meta = {
        "name": name or "-", "ucid": ucid or "-", "channel_url": channel_url,
        "channel_count": len(entries), "raw_count": len(ids),
        "source_desc": ("GitHub Actions 美国节点直连；全量枚举(videos/shorts/streams) "
                        "+ 多源日期解析(RSS/官方接口/镜像/播放接口/相对文本) + 历史缓存逐周收敛"),
        "elapsed": "%d分%.0f秒" % (elapsed // 60, elapsed % 60),
        "counts": counts,
        "prec_by_src": ", ".join("%s=%d" % (SRC_DESC.get(k, k), v)
                                 for k, v in sorted(by_src.items(), key=lambda x: -x[1])),
    }
    out = build(entries, meta, out, prev_path=prev)
    n = save_date_cache(cache_p, {e["id"]: (e.get("ts_ms"), e.get("prec"), e.get("src"))
                                  for e in entries})
    log("[缓存] 写入 %s（%d 条）" % (cache_p, n))
    return {"out": out, "name": name or "-", "total": len(entries),
            "stats": by_src, "deadline_left": deadline.left()}


# ------------------------------------------------ 频道列表

def load_channels(cli_url):
    """channels.txt 里的频道 + 命令行额外传入的频道（去重，保持顺序）。"""
    urls = []
    if os.path.isfile(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.split("#")[0].strip()
                    if s:
                        urls.append(s)
        except Exception as ex:
            log("[频道表] 读取 %s 失败：%s" % (CHANNELS_FILE, str(ex)[:80]))
    if cli_url:
        urls.append(cli_url)
    elif not urls:
        urls.append(os.environ.get("CHANNEL_URL") or DEFAULT_CHANNEL)
    seen, out = set(), []
    for u in urls:
        k = ensure_base(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


# ------------------------------------------------ 离线演练

def run_demo(args):
    """离线演练：内置样例跑通 归并->排序->输出->RSS落盘 全流程，供本地测试。"""
    SAMPLE = [
        # (id, 标题, 官方日YYYY-MM-DD, RSS精确ISO, tags, 时长秒)
        ("JNxQq3KFEM4",
         "Dissolving $1000 of Platinum to Make $6000 of Chloroplatinic Acid for Professional Use",
         "2024-12-24", "2024-12-24T15:44:46+00:00", ["直播/回放 Live"], 5072),
        ("3YwnlYl0VxA", "This Candle MAKES Oxygen and Started a Fire on a Space Station",
         "2024-12-20", "", [], 332),
        ("_d1J9MVkRzM", "Refuel a Glow Stick", "2024-06-13", "", ["短视频 Shorts"], 12),
        ("9p3So4ijD4U", "Refuel a Glow Stick", "2024-05-30", "", [], 8),
        ("zLWEemhtdbE", "", "2023-05-02", "", [], None),          # 标题待补，仅官方日
        ("GsN7r6QkpRA", "Lab Notes - Cleaving Sodium Metal - March 27th 2019", "", "", [], 1371),
        ("ZxCO9BaBBHg", "Chemical Thunderstorm in a Beaker (April 2018)", "", "", [], 601),
        ("a1b2c3d4e5f", "Early Lab Notes - Something", "", "", [], None),   # 相对时间兜底
        ("gjsMV1MglA4", "Mystery Video", "", "", [], None),      # 彻底的未知
    ]
    entries = [{"id": v, "title": t, "duration": dur, "tags": list(g)}
               for v, t, u, iso, g, dur in SAMPLE]
    api, rss = {}, {}
    for v, t, u, iso, g, dur in SAMPLE:
        if u:
            api[v] = (int(dt.datetime.strptime(u, "%Y-%m-%d")
                          .replace(tzinfo=timezone.utc).timestamp() * 1000),
                      "day_official", "innertube")
        if iso:
            rss[v] = (parse_publish_date(iso), "exact", "rss")
    DEMO_REL = {"a1b2c3d4e5f":
                (int(dt.datetime(2016, 4, 3, tzinfo=timezone.utc).timestamp() * 1000), "year")}
    ucid = "DEMOUC1"
    rss_body = ('<?xml version="1.0"?><feed>'
                '<entry><id>yt:video:JNxQq3KFEM4</id><yt:videoId>JNxQq3KFEM4</yt:videoId>'
                '<published>2024-12-24T15:44:46+00:00</published>'
                '<title>Dissolving $1000 of Platinum</title></entry></feed>')
    os.makedirs(os.path.join("cache", ucid, "pages"), exist_ok=True)
    with open(os.path.join("cache", ucid, "pages", "rss.xml"), "wb") as f:
        f.write(rss_body.encode("utf-8"))
    merge_dates(entries, [("rss", rss), ("innertube", api)])
    apply_title_and_relative(entries, DEMO_REL)
    out = args.out or "data/_demo_videos.txt"
    meta = {"name": "NurdRage(演示)", "ucid": ucid, "channel_url": DEFAULT_CHANNEL,
            "channel_count": len(SAMPLE), "source_desc": "演示样例（离线段）",
            "raw_count": len(SAMPLE), "elapsed": "0分0秒",
            "counts": [("官方 RSS（精确到秒）", len(rss)),
                       ("YouTube 播放接口（日精度）", len(api))],
            "prec_by_src": "rss=1, innertube=5, title=2, relative=1, unknown=1"}
    return build(entries, meta, out)


# ------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(
        description="GitHub Actions 云端全量抓取器（国内免梯子；跑在 GitHub 美国节点直连 YouTube）")
    ap.add_argument("url", nargs="?", default=None,
                    help='追加抓取的频道链接，如 "https://www.youtube.com/@NurdRage/videos"')
    ap.add_argument("--out", default=None, help="输出 txt 路径（默认 data/<频道>_videos.txt）")
    ap.add_argument("--list-only", action="store_true", help="只枚举 ID+标题，不取日期")
    ap.add_argument("--with-upload-dates", action="store_true",
                    help="额外用 yt-dlp 客户端轮换逐条取官方日（慢，默认关）")
    ap.add_argument("--with-flat-dates", action="store_true",
                    help="兼容旧参数（已并入默认流程，可忽略）")
    ap.add_argument("--with-wayback", action="store_true",
                    help="额外跑 Wayback 快照并集兜底（archive.org 常拒连，默认关）")
    ap.add_argument("--demo", action="store_true", help="离线演练：内置样例跑通全流程（本地测试用）")
    ap.add_argument("--budget-min", type=float, default=None,
                    help="本次运行的日期抓取时间预算（分钟），默认 45")
    args = ap.parse_args()
    try:
        if args.demo:
            run_demo(args)
            return
        deadline = Budget((args.budget_min or
                           float(os.environ.get("TIME_BUDGET_MIN", "45"))) * 60)
        channels = load_channels(args.url)
        log("== 本次将抓取 %d 个频道（总预算 %.0f 分钟）=="
            % (len(channels), deadline.left() / 60))
        for u in channels:
            log("   - %s" % u)
        if args.out and len(channels) > 1:      # 指定单个输出文件时只抓第一个，保持旧行为
            channels = channels[:1]
        results = []
        for i, u in enumerate(channels, 1):
            if i > 1 and not deadline.ok(120):
                log("[预算] 剩余时间不足以再抓一个频道，跳过后续 %d 个"
                    % (len(channels) - i + 1))
                break
            # 公平分配：每个频道最多拿走"剩余预算的 7 成再均分"，且不超过 10 分钟，
            # 保证 5 个频道都拿得到日期通道，而不是第一个吃光全部预算。
            sub = Budget(int(min(600, max(240, deadline.left() * 0.7 / (len(channels) - i + 1)))),
                         parent=deadline)
            log("\n[预算] 频道 %d/%d 分配 %.0f 秒（全局剩余 %.0f 秒）"
                % (i, len(channels), sub.limit, deadline.left()))
            try:
                r = run_channel(u, args, sub)
                if r:
                    results.append(r)
            except Exception as ex:
                log("[错误] 频道 %s 抓取失败：%s" % (u, str(ex)[:200]))
        if not results:
            log("[错误] 所有频道均未产出结果。")
            sys.exit(4)
        log("\n" + "=" * 60)
        log("== 本次共产出 %d 个清单 ==" % len(results))
        for r in results:
            log("  - %s（%d 条）" % (r["out"], r["total"]))
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
