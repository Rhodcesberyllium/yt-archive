#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py — GitHub Actions 云端全量抓取器（国内免梯子的"一劳永逸"方案）。

为什么在云端跑？
  本机在中国直连 YouTube 被墙、公共 CORS 中继又时好时坏、Wayback 快照也常被墙。
  本脚本运行在 GitHub Actions 的美国服务器上，直连 YouTube —— 无墙、无中继、无 VPN，
  每周定时全量枚举频道视频（ID + 标题 + 发布时间），结果写回仓库，墙内随时可下载。

用法：
  python fetch.py                                  # 用 CHANNEL_URL 环境变量或内置默认频道
  CHANNEL_URL="https://www.youtube.com/@xxx/videos" python fetch.py
  python fetch.py "https://www.youtube.com/@xxx/videos"
  python fetch.py --list-only                      # 只枚举 ID+标题（快，日期仅靠标题/相对文本）
  python fetch.py --demo                           # 离线演练：内置样例跑通全流程（本地测试用）

输出：默认 data/<频道>_videos.txt，格式与本地版完全一致，可直接用 verify_output.py 验收。
依赖：yt-dlp（pip install yt-dlp）；频道页/RSS 用标准库 urllib 直取，零第三方依赖。

日期来源优先级（与本地版一致，绝不伪造精确日期）：
  1) 精确到秒   —— 官方 RSS <published>（最新约 15 条）
  2) 日精度     —— 逐个视频官方 upload_date（YYYYMMDD，来自 watch 元数据；被风控时自动换客户端重试）
  3) 标题推断   —— 标题内嵌完整日期（如 "March 27th 2019"），标注"推断"
  4) 相对文本   —— 频道页相对时间（"3 years ago"），精确到年/月/周/日，标注粒度（兜底主力）
  5) 未知       —— 以上全拿不到才标未知，下周自动重跑会再试

云端 IP 特点说明：GitHub 数据中心 IP 常被 YouTube 对"单个视频 watch 页"风控，
但"频道标签页/列表"通常不拦。因此 2) 会被 4) 兜住，任何情况下都有可见日期。
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
from datetime import timezone, timedelta

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


def http_get_bytes(url, timeout=40):
    """用标准库直取页面/API（云端直连）。成功返回 bytes，失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as ex:
        log("[网络] 获取失败 %s: %s" % (url[:70], str(ex)[:90]))
        return None


def ensure_base(channel_url):
    for suf in ("/videos", "/shorts", "/streams", "/featured", "/playlists"):
        channel_url = channel_url.split(suf)[0]
    return channel_url.rstrip("/")


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


def parse_ago_unit(text):
    """'3 years ago' -> (秒数, 精度单位)；解析失败返回 (None, None)"""
    m = re.search(r"(\d+)\s+(year|month|week|day|hour|minute|second)s?\s+ago",
                  (text or "").lower())
    if not m:
        return (None, None)
    return (int(m.group(1)) * AGO_UNITS[m.group(2)], m.group(2))


def clean_title(t):
    """清理标题残留（" - YouTube" 后缀、多余空白）"""
    s = (t or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?:\s*[-–—]\s*)?YouTube$", "", s).strip()
    return re.sub(r"\s+", " ", s)


def parse_title_date(title):
    """从标题解析完整日期 -> (epoch_ms, 'day_title'|'month_title') 或 None。
    只在标题含完整年份时推断（无年份的月+日不加假装精确，留给相对文本/未知）。"""
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


# ------------------------------------------------ 取数（全部跑在 GitHub 美国节点）

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


def channel_meta(channel_url, ids):
    """从频道 /videos 页解析 (ucid, name)。HTML 失败时退化为 yt-dlp 枚举结果。
    返回 (ucid 或 None, name 或 None)。"""
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
            name = re.sub(r"\s*-\s*YouTube\s*$", "",
                          t.group(1).decode("utf-8", "replace")).strip()
        if not name:
            m2 = re.search(rb'"title":"([^"]{1,200})","navigationEndpoint"', html)
            if m2:
                name = m2.group(1).decode("utf-8", "replace").strip().replace("\\u0026", "&")
        if not name:
            # 从枚举结果里取最常见的频道名兜底
            pass
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
                name = b.strip()
    return ucid, name


def fetch_upload_dates(ids, chunk=60):
    """逐个视频取官方 upload_date(YYYYMMDD)，返回 {id: 'YYYYMMDD'}。
    策略：被云 IP 风控时会早退止损，再用 web_embedded 客户端对缺失项重试一次。"""
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
        done = min(i + chunk, n)
        log("[日期] %d/%d（当前命中 %d）" % (done, n, len(res)))
        if done >= chunk and len(res) < 0.25 * done:
            log("[日期] 命中率过低(%d/%d)，疑被风控，跳过剩余 %d 条；由频道页相对时间兜底"
                % (len(res), done, n - done))
            break
    # 仅当首批真实返回过日期（说明该 IP 未被整体风控）才做嵌入式客户端重试；
    # 实测云端 web/embedded 均全程 0，重试纯属白等。
    if res and len(res) < 0.3 * n:
        missing = [v for v in ids_list if v not in res]
        log("[日期] 用 web_embedded 客户端重试 %d 条缺失项" % len(missing))
        for j in range(0, len(missing), 40):
            urls = ["https://www.youtube.com/watch?v=%s" % v for v in missing[j:j + 40]]
            out = yt_run(["--no-playlist", "--print", "%(id)s\t%(upload_date)s",
                          "--extractor-args", "youtube:player_client=web_embedded",
                          "--retries", "2"] + urls, timeout=3600)
            for line in out.splitlines():
                if "\t" not in line:
                    continue
                vid, d = line.split("\t", 1)
                d = d.strip()
                if VIDEOID_RE.fullmatch(vid.strip()) and re.fullmatch(r"\d{8}", d):
                    res[vid.strip()] = d
    return res


def fetch_rss(ucid, dst):
    """官方 RSS：最近约 15 条，带精确到秒的发布时间与官方标题。
    返回 (ts_map, title_map)，同时把原始 XML 落盘（供 verify_output.py 交叉对照）。
    YouTube 偶发只返回几条，重试一次抗波动。"""
    out, titles = {}, {}
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + ucid
    for attempt in (1, 2):
        body = http_get_bytes(url)
        if not body:
            if attempt == 2:
                log("[RSS] 获取失败（频道 ID：%s）" % ucid)
            else:
                time.sleep(1)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(body)
        out, titles = {}, {}
        for vid, pub, title in re.findall(
                rb"<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)</published>"
                rb".*?<title>([^<]+)</title>", body, re.S):
            vid = vid.decode("utf-8", "replace")
            ts = parse_publish_date(pub.decode("utf-8", "replace"))
            if ts:
                out[vid] = ts
            t = title.decode("utf-8", "replace").strip()
            for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&#x27;", "'"), ("&apos;", "'")):
                t = t.replace(a, b)
            if t:
                titles[vid] = t
        if len(out) >= 8:
            break
        if attempt == 1:
            log("[RSS] 仅有 %d 条，稍候重试一次" % len(out))
            time.sleep(1)
    log("[RSS] 获得 %d 条精确日期" % len(out))
    return out, titles


# ---------------- 频道页相对时间（云 IP 被 watch 页风控时的兜底主力） ----------------

def extract_rel_from_data(o, anchor_ms, rel=None, tokens=None, titles=None):
    """遍历任意频道/续拉 JSON：提取 (videoId/contentId -> (相对毫秒, 精度))，
    同时收集 continuationCommand 的翻页 token，以及**同一 renderer 内的标题**
    （id↔标题取自同一对象，自洽不错位，避免网格枚举顶格串位）。
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
    """从频道标签页 ytInitialData 提取 (rel, tokens)。处理 \x22 双转义。"""
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
    """兼容旧函数：返回 {id: (ms, 精度)}，含旧布局邻近匹配兜底。"""
    rel, _, _ = extract_rel_from_html_data(html, anchor_ms)
    if not rel:
        for mm in re.finditer(rb'"videoId":"([A-Za-z0-9_-]{11})"', html):
            start = max(0, mm.start() - 2500)
            window = html[start:mm.start() + 2500]
            agos = re.findall(rb'(\d+\s+(?:year|month|week|day|hour|minute|second)s?\s+ago)',
                              window)
            if agos:
                secs, prec = parse_ago_unit(agos[0].decode())
                if secs:
                    rel[mm.group(1).decode()] = (int(anchor_ms - secs * 1000), prec)
    return rel


DEFAULT_ITV_VERSION = "2.20250310.00.00"

# InnerTube 候选客户端（按优先序）：页面自带版本 > 静态 WEB > ANDROID
WEB_CLIENTS = [("WEB", None), ("WEB", "2.20250310.00.00"), ("ANDROID", "19.09.37")]


def post_browse(token, client_name, client_version, api_key):
    """InnerTube browse 接口 POST 翻页续拉。返回 (data|None, 错误文本|None)。"""
    url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
    if api_key:
        url += "&key=" + urllib.parse.quote(api_key)
    client = {"clientName": client_name, "clientVersion": client_version,
              "hl": "en", "gl": "US"}
    if client_name == "ANDROID":
        client["androidSdkVersion"] = 30
    payload = {"context": {"client": client}, "continuation": token}
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if client_name == "WEB":
        headers["X-Youtube-Client-Name"] = "1"
        headers["X-Youtube-Client-Version"] = client_version
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except Exception as ex:
        return None, str(ex)[:120]


def _resp_diag(data):
    """从 InnerTube 响应里抽取诊断要点，便于一眼看出是 token 问题还是被拒。"""
    if not isinstance(data, dict):
        return "非JSON对象"
    keys = ",".join(list(data.keys())[:8])
    err = data.get("error")
    if isinstance(err, dict):
        return "ERROR(%s:%s) keys=[%s]" % (
            err.get("code"), str(err.get("message"))[:90], keys)
    n = 0
    try:
        n = len(data.get("onResponseReceivedActions", []) or [])
    except Exception:
        n = -1
    return "keys=[%s] onResponseReceivedActions=%d" % (keys, n)


def fetch_inner_relative_dates(channel_url, anchor_ms, max_pages=60):
    """核心兜底：三 tab 页面 + InnerTube 翻页续拉，给尽量多视频标上相对时间。

    关键改进（针对上次"只翻 1 页且并集不长"）：
      - 页面可能含多个 continuation token（grid + Shorts 栏等），逐个候选尝试；
      - 每个 token 依次用 3 种客户端（页面版WEB -> 静态WEB -> ANDROID）兜底；
      - 每次 POST 都打诊断日志（响应键 / 条目数 / 下页token），失败只丢当页。"""
    base = ensure_base(channel_url)
    rel, titles = {}, {}
    for tab in ("videos", "shorts", "streams"):
        html = http_get_bytes("%s/%s" % (base, tab))
        if not html:
            continue
        page_ver = ""
        mcv = re.search(rb'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        if mcv:
            page_ver = mcv.group(1).decode()
        key = ""
        mk = re.search(rb'"INNERTUBE_API_KEY":"([^"]+)"', html)
        if mk:
            key = mk.group(1).decode()
        rel0, tokens, titles0 = extract_rel_from_html_data(html, anchor_ms)
        for k, v in rel0.items():
            rel.setdefault(k, v)
        for k, v in titles0.items():
            titles.setdefault(k, v)
        candidates = list(dict.fromkeys(tokens))[:6]
        queue, used = list(candidates), set()
        pages, diags = 0, []
        while queue and pages < max_pages:
            token = queue.pop(0)
            if token in used:
                continue
            used.add(token)
            ok = False
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
                    diags.append("[%s/%s] 新条目%d, 下页token:%s" % (
                        cname, cver, len(relp), "有" if nxt else "无"))
                    pages += 1
                    ok = True
                    break
                diags.append("[%s/%s] %s" % (cname, cver, _resp_diag(data)))
            time.sleep(0.3)
        log("[相对时间] tab=%s 首屏%d条 + 翻页%d页, 候选token%d个, 并集%d" % (
            tab, len(rel0), pages, len(candidates), len(rel)))
        if not pages:
            for d in diags[:5]:
                log("[相对时间]    诊断: %s" % d)
    return rel, titles


def fetch_yt_flat_dates(channel_url, ucid, wanted_ids):
    """尝试用 yt-dlp 平板模式带出 release_date/timestamp（yt-dlp 内部会自动翻页，
    走的是它经打的续拉逻辑）。若覆盖率够高（>=60%）返回 {id: 'YYYYMMDD'}。"""
    base = ensure_base(channel_url)
    urls = [base + "/videos"]
    if ucid and ucid.startswith("UC"):
        urls.append("https://www.youtube.com/playlist?list=UU" + ucid[2:])
    res = {}
    want = set(wanted_ids) or None
    for url in urls:
        out = yt_run(["--flat-playlist",
                      "--print", "%(id)s\t%(title)s\t%(release_date)s\t%(timestamp)s",
                      url], timeout=1800)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            vid = parts[0].strip()
            rd = (parts[2] or "").strip()
            if VIDEOID_RE.fullmatch(vid) and re.fullmatch(r"\d{8}", rd):
                if want is None or vid in want:
                    res[vid] = rd
        cov = 100.0 * len(res) / max(1, len(want or []))
        log("[官方日] yt-dlp平板探测 %s覆盖 %s%%（%d条）" % (url.split("/")[-2] or url, "%.0f" % cov, len(res)))
        if len(res) >= 0.6 * max(1, len(want or [])):
            break
    return res


def _parse_wb_ts(ts):
    """Wayback 时间戳 -> epoch ms；失败返回 None。"""
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return int(dt.datetime.strptime(ts[:len(dt.datetime.now().strftime(fmt))], fmt)
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            continue
    return None


def fetch_wayback_relative(ucid, handle, anchor_ms, max_snapshots=24):
    """Wayback 快照并集：收集频道各 URL 形式的历史快照，用各快照当日的相对时间
    还原当时可见的视频（云端直连可达；专治 channels 里连 InnerTube 都翻不出的残尾）。"""
    cand_urls = []
    if ucid:
        cand_urls.append("https://www.youtube.com/channel/%s/videos" % ucid)
    if handle:
        cand_urls.append("https://www.youtube.com/@%s/videos" % handle)
    snaps = []
    for u in cand_urls:
        cdx = ("http://web.archive.org/cdx/search/cdx?url=%s&output=json"
               "&fl=timestamp,statuscode&filter=statuscode:200&collapse=timestamp:6&limit=80"
               % urllib.parse.quote(u, safe=""))
        body = http_get_bytes(cdx)
        if not body:
            continue
        try:
            rows = json.loads(body)
        except Exception:
            continue
        got = 0
        for row in rows[1:]:
            if row and str(row[0]).isdigit():
                snaps.append((str(row[0]), u))
                got += 1
        if got:
            log("[Wayback] %s 命中 %d 个快照" % (u, got))
    snaps = sorted(set(snaps))
    if len(snaps) > max_snapshots:      # 时间轴均匀抽样，控制耗时
        step = len(snaps) / max_snapshots
        snaps = [snaps[int(i * step)] for i in range(max_snapshots)]
    log("[Wayback] 将抓取 %d 个快照" % len(snaps))
    rel = {}
    n = len(snaps)
    for i, (ts, u) in enumerate(snaps, 1):
        wb_url = "https://web.archive.org/web/%sid_/%s" % (ts, u)
        html = http_get_bytes(wb_url)
        if not html:
            continue
        an = _parse_wb_ts(ts) or anchor_ms
        got = extract_rel_from_html(html, an)
        for k, v in got.items():
            rel.setdefault(k, v)
        if i % 6 == 0 or i == n:
            log("[Wayback] 快照 %d/%d（并集 %d）" % (i, n, len(rel)))
    return rel


def apply_relative(entries, rel):
    """把没有真实/标题日期的条目用相对文本兜底（升格为 approx，标注粒度）。"""
    up = 0
    for e in entries:
        if e.get("prec") and e["prec"] != "unknown":
            continue
        if e["id"] in rel:
            ms, prec = rel[e["id"]]
            e["approx_ms"], e["approx_prec"], e["prec"] = ms, prec, "approx"
            up += 1
        else:
            e["prec"] = "unknown"
    log("[相对时间] 兜底 %d 条未知 → 相对时间粒度" % up)
    return up


# ------------------------------------------------ 日期归并与排序

def assign_dates(entries, rss_map):
    """按可信度给每条定精度：RSS精确秒 > 官方日 > 标题完整日期 > (相对文本由 apply_relative 兜底)。"""
    for e in entries:
        if e.get("rss_ts"):
            e["ts_ms"], e["prec"] = e["rss_ts"], "exact"
        elif e.get("upload_date"):
            ud = e["upload_date"]
            if re.fullmatch(r"\d{8}", ud):
                y, m, d = int(ud[:4]), int(ud[4:6]), int(ud[6:8])
                e["ts_ms"] = int(dt.datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
                e["prec"] = "day_official"
        if e.get("prec") in (None, "unknown") and not e.get("ts_ms"):
            pair = parse_title_date(e.get("title") or "")
            if pair:
                e["ts_ms"], e["prec"] = pair
            else:
                e["prec"] = "unknown"


def _sort_key(e):
    ts = e.get("ts_ms") or e.get("approx_ms")
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
    approx = sum(1 for e in entries if e.get("prec") == "approx")
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
        elif e.get("prec") == "approx":
            L.append(format_approx(e["approx_ms"], e.get("approx_prec") or "year"))
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
    L.append("其中官方日精度日期: %d 条" % day_o)
    L.append("其中标题推断日期(日/月): %d / %d 条" % (title_d, title_m))
    L.append("其中相对文本推断(年/月/周/日): %d 条" % approx)
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
    print("\n[完成] %s：共 %d 条（精确 %d / 官方日 %d / 标题推断 %d / 相对文本 %d / 未知 %d），耗时 %s"
          % (out, len(entries),
             sum(e.get("prec") == "exact" for e in entries),
             sum(e.get("prec") == "day_official" for e in entries),
             sum(e.get("prec") in ("day_title", "month_title") for e in entries),
             sum(e.get("prec") == "approx" for e in entries),
             unknown, meta["elapsed"]), flush=True)
    return out


def run_demo(args):
    """离线演练：内置样例数据跑通 归并->排序->输出->RSS落盘 全流程，供本地测试。"""
    SAMPLE = [
        # (id, 标题, 官方日YYYY-MM-DD, RSS精确ISO, tags)
        ("JNxQq3KFEM4",
         "Dissolving $1000 of Platinum to Make $6000 of Chloroplatinic Acid for Professional Use",
         "2024-12-24", "2024-12-24T15:44:46+00:00", ["直播/回放 Live"]),
        ("3YwnlYl0VxA", "This Candle MAKES Oxygen and Started a Fire on a Space Station",
         "2024-12-20", "", []),
        ("_d1J9MVkRzM", "Refuel a Glow Stick", "2024-06-13", "", ["短视频 Shorts"]),
        ("9p3So4ijD4U", "Refuel a Glow Stick", "2024-05-30", "", []),
        ("zLWEemhtdbE", "", "2023-05-02", "", []),         # 标题待补，仅官方日
        ("GsN7r6QkpRA", "Lab Notes - Cleaving Sodium Metal - March 27th 2019", "", "", []),
        ("ZxCO9BaBBHg", "Chemical Thunderstorm in a Beaker (April 2018)", "", "", []),
        ("a1b2c3d4e5f", "Early Lab Notes - Something", "", "", []),   # 靠相对时间兜底
        ("gjsMV1MglA4", "Mystery Video", "", "", []),     # 彻底的未知
    ]
    entries = [{"id": v, "title": t, "upload_date": u, "rss_ts": None,
                "prec": None, "tags": list(g)} for v, t, u, _, g in SAMPLE]
    for (v, t, u, iso, g) in SAMPLE:
        e = next(x for x in entries if x["id"] == v)
        if iso:
            e["rss_ts"] = parse_publish_date(iso)
        if u:
            e["upload_date"] = u.replace("-", "")
    rss = {v: parse_publish_date(iso) for v, _, _, iso, _ in SAMPLE if iso}
    rss = {k: v for k, v in rss.items() if v}
    # 模拟频道页相对时间解析结果（对应 a1b2c3d4e5f 无任何官方/标题日期的情况）
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
    assign_dates(entries, rss)
    apply_relative(entries, DEMO_REL)
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
    ucid, name = channel_meta(channel_url, ids)
    rss_map, rss_titles = {}, {}
    if ucid:
        try:
            rss_map, rss_titles = fetch_rss(ucid, os.path.join("cache", ucid, "pages", "rss.xml"))
        except Exception as ex:
            log("[警告] RSS 获取失败: %s" % str(ex)[:120])
    else:
        log("[RSS] 未解析到频道 ID，跳过（不影响其它日期通道）")
    for e in entries:
        if e["id"] in rss_map:
            e["rss_ts"] = rss_map[e["id"]]
        # RSS 标题只用于"填空"：RSS 快照可能是发布时的旧标题（视频后改过名），
        # 绝不能覆盖网格/InnerTube 的现行标题（曾因此把顶格视频标题改错）。
        if e["id"] in rss_titles and rss_titles[e["id"]] and not (e.get("title") or "").strip():
            e["title"] = rss_titles[e["id"]]
    if not args.list_only:
        # 逐条官方日（云端 IP 常被整段风控，默认关；需要时 --with-upload-dates）
        if args.with_upload_dates:
            dates = fetch_upload_dates([e["id"] for e in entries])
            for e in entries:
                if e["id"] in dates:
                    e["upload_date"] = dates[e["id"]]
        # yt-dlp 平板官方日探测（同样常被风控，默认关；--with-flat-dates）
        if args.with_flat_dates:
            try:
                flat_dates = fetch_yt_flat_dates(channel_url, ucid, [e["id"] for e in entries])
                backfill = 0
                for e in entries:
                    if not e["upload_date"] and e["id"] in flat_dates:
                        e["upload_date"] = flat_dates[e["id"]]
                        backfill += 1
                if backfill:
                    log("[官方日] yt-dlp 平板模式补入 %d 条" % backfill)
            except Exception as ex:
                log("[警告] yt-dlp 平板日期探测失败: %s" % str(ex)[:100])
    else:
        log("[提示] --list-only：跳过日期通道，仅标题/相对时间。")
    assign_dates(entries, rss_map)
    anchor_ms = int(dt.datetime.now(timezone.utc).timestamp() * 1000)
    rel, ititles = fetch_inner_relative_dates(channel_url, anchor_ms)
    # InnerTube 每条 renderer 里自洽的 id↔标题（同一对象取出，不会串位），
    # 覆盖掉网格枚举顶格错配 / RSS 旧标题的问题。
    fixed = 0
    for e in entries:
        t = (ititles.get(e["id"]) or "").strip()
        if t and t != e.get("title"):
            e["title"] = t
            fixed += 1
    if fixed:
        log("[标题] InnerTube 同源修正 %d 条" % fixed)
    # Wayback 快照并集（archive.org 在云 IP 上常拒连/超时且零收益，默认关；--with-wayback 才跑）
    if args.with_wayback:
        try:
            handle = ""
            mh = re.search(r"youtube\.com/(?:@|c/|user/)([^/?#]+)", channel_url)
            if mh:
                handle = mh.group(1).strip()
            wb = fetch_wayback_relative(ucid, handle, anchor_ms)
            for k, v in wb.items():
                rel.setdefault(k, v)
        except Exception as ex:
            log("[Wayback] 失败: %s" % str(ex)[:100])
    apply_relative(entries, rel)
    elapsed = dt.datetime.now() - t0
    out = args.out or ("data/%s_videos.txt"
                       % re.sub(r'[\\/:*?"<>|]+', "_", (name or "channel"))[:60])
    src = ("GitHub Actions 美国节点直连 YouTube；全量枚举(videos/shorts/streams) + "
           "官方 RSS（精确秒） + 频道页相对时间（InnerTube 翻页兜底）")
    meta = {"name": name or "-", "ucid": ucid or "-", "channel_url": channel_url,
            "channel_count": len(entries), "source_desc": src,
            "raw_count": len(entries),
            "elapsed": "%d分%.0f秒" % (elapsed.seconds // 60, elapsed.seconds % 60)}
    return build(entries, meta, out, rss_map)


def main():
    ap = argparse.ArgumentParser(
        description="GitHub Actions 云端全量抓取器（国内免梯子；跑在 GitHub 美国节点直连 YouTube）")
    ap.add_argument("url", nargs="?", default=None,
                    help='频道链接，如 "https://www.youtube.com/@NurdRage/videos"')
    ap.add_argument("--out", default=None, help="输出 txt 路径（默认 data/<频道>_videos.txt）")
    ap.add_argument("--list-only", action="store_true", help="只枚举 ID+标题，不取日期")
    ap.add_argument("--with-upload-dates", action="store_true",
                    help="逐条抓官方日精度（云端 IP 常被风控，默认关）")
    ap.add_argument("--with-flat-dates", action="store_true",
                    help="yt-dlp 平板模式探测官方日（默认关）")
    ap.add_argument("--with-wayback", action="store_true",
                    help="额外跑 Wayback 快照并集兜底（archive.org 在云 IP 上常拒连，默认关）")
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
