#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_sources.py — 在 GitHub Actions 真实节点上探测"哪些来源能拿到官方发布日期"。

目的：不靠猜测决定 fetch.py 的取日期策略。本脚本只读不写（除了它自己的报告），
把每个候选来源在当前云端 IP / 当前网络下的真实表现打成一份 markdown 报告。

用法：python probe_sources.py > probe/report.md
"""
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 真实样本：NurdRage 291 条里取新/中/老各一条（ID 取自仓库现有产出）
UCID = "UCIgKGGJkt1MrNmhq3vRibYA"
SAMPLES = [
    ("新", "JNxQq3KFEM4"),   # 2024-12-24（RSS 有）
    ("中", "GsN7r6QkpRA"),   # 标题含 2019-03-27
    ("老", "GZxaXH70k5o"),   # 列表最末
]

PIPED = ["pipedapi.kavin.rocks", "api.piped.private.coffee", "pipedapi.adminforge.de",
         "pipedapi.leptons.xyz", "pipedapi.ducks.party", "piped-api.lunar.icu",
         "pipedapi.reallyaweso.me", "pipedapi.drgns.space", "api.piped.projectsegfau.lt",
         "pipedapi.smnz.de", "pipedapi.orangenet.cc", "pipedapi.zeteo.dev",
         "piped-api.privacy.com.de", "pipedapi.astartes.nl", "pipedapi.bpwn.ro"]

INVIDIOUS = ["inv.nadeko.net", "invidious.nerdvpn.de", "yewtu.be", "invidious.f5.si",
             "inv.tux.pizza", "invidious.privacyredirect.com", "iv.melmac.space",
             "invidious.jing.rocks", "invidious.reallyaweso.me", "invidious.dhusch.de",
             "invidious.perennialte.ch", "iv.datura.network", "invidious.materialio.us"]

# yt-dlp 客户端轮换：写清 clientVersion 便于复现
IT_CLIENTS = [
    ("ANDROID", "19.09.37", {"androidSdkVersion": 30}),
    ("ANDROID_VR", "1.60.19", {"androidSdkVersion": 32}),
    ("IOS", "19.09.3", {"deviceMake": "Apple", "deviceModel": "iPhone16,2",
                        "osName": "iPhone", "osVersion": "17.5.1.21F90"}),
    ("TVHTML5", "7.20240304.10.00", {}),
    ("TVHTML5_SIMPLY_EMBEDDED_PLAYER", "2.0", {}),
    ("WEB_EMBEDDED_PLAYER", "1.20240303.00.00", {}),
    ("MWEB", "2.20240304.08.00", {}),
    ("WEB", "2.20250310.00.00", {}),
]

YTDLP_CLIENTS = ["default", "web", "web_safari", "mweb", "tv", "tv_embedded",
                 "ios", "android", "android_vr", "web_embedded", "android_producer"]

out = []


def say(s=""):
    out.append(s)
    print(s, flush=True)


def http(url, timeout=20, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), None, time.time() - t0
    except Exception as ex:
        return None, "%s" % str(ex)[:110], time.time() - t0


def date_of(obj):
    """从 player 响应里挖 publishDate/uploadDate。"""
    try:
        mm = (obj.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        for k in ("publishDate", "uploadDate"):
            v = mm.get(k)
            if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                return v
    except Exception:
        pass
    return None


def status_of(obj):
    try:
        return ((obj.get("playabilityStatus") or {}).get("status") or "?")
    except Exception:
        return "?"


def ytdlp(args, timeout=180):
    cmd = (["yt-dlp", "--no-warnings", "--ignore-errors", "--no-cache-dir",
            "--skip-download"] + args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or "", p.returncode
    except subprocess.TimeoutExpired:
        return "", -9


# ------------------------------------------------------------------ 0. 基线
def probe_env():
    say("## 0. 运行环境基线\n")
    so, rc = ytdlp(["--version"], timeout=60)
    say("- yt-dlp 版本: `%s` (rc=%s)" % (so.strip() or "未知", rc))
    body, err, _ = http("https://www.youtube.com/feeds/videos.xml?channel_id=" + UCID)
    say("- 官方 RSS: %s" % ("可达，%d 字节" % len(body) if body else "失败 " + err))
    if body:
        n = len(re.findall(rb"<yt:videoId>", body))
        say("- RSS 条数: %d（官方上限约 15）" % n)
    body, err, _ = http("https://www.youtube.com/channel/%s/videos" % UCID)
    say("- 频道页 HTML: %s" % ("可达，%d 字节" % len(body) if body else "失败 " + err))
    if body:
        has = bool(re.search(rb'"dateText"|"publishDate"', body))
        say("- 频道页 HTML 含日期字段: %s" % has)


# ------------------------------------------------- 1. yt-dlp 客户端轮换
def probe_ytdlp():
    say("\n## 1. yt-dlp 各 player_client 取 upload_date（%d 个样本视频）\n" % len(SAMPLES))
    say("| client | 命中/总数 | 取到的日期 | rc |")
    say("|---|---|---|---|")
    urls = ["https://www.youtube.com/watch?v=%s" % v for _, v in SAMPLES]
    for c in YTDLP_CLIENTS:
        args = ["--no-playlist", "--print", "%(id)s\t%(upload_date)s\t%(release_date)s",
                "--retries", "2"]
        if c != "default":
            args += ["--extractor-args", "youtube:player_client=" + c]
        so, rc = ytdlp(args + urls, timeout=240)
        got, dates = 0, []
        for line in so.splitlines():
            if "\t" not in line:
                continue
            parts = line.split("\t")
            vid = parts[0].strip()
            vals = [(parts[i] or "").strip() for i in range(1, min(3, len(parts)))]
            hit = next((v for v in vals if re.fullmatch(r"\d{8}", v)), "")
            if hit:
                got += 1
                dates.append("%s=%s" % (vid, hit))
        say("| %s | %d/%d | %s | %s |" % (c, got, len(SAMPLES),
                                          ", ".join(dates) or "—", rc))


# ------------------------------------------------- 2. InnerTube player POST
def probe_innertube():
    say("\n## 2. InnerTube /youtubei/v1/player POST 各客户端（免 key）\n")
    body, err, _ = http("https://www.youtube.com/channel/%s/videos" % UCID)
    key = ""
    if body:
        mk = re.search(rb'"INNERTUBE_API_KEY":"([^"]+)"', body)
        if mk:
            key = mk.group(1).decode()
    say("- 页面取到的 INNERTUBE_API_KEY: `%s`\n" % (key[:12] + "..." if key else "未取到"))
    say("| client | sdk | playabilityStatus | 取到的 publishDate |")
    say("|---|---|---|---|")
    for name, ver, extra in IT_CLIENTS:
        url = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
        if key:
            url += "&key=" + urllib.parse.quote(key)
        client = {"clientName": name, "clientVersion": ver, "hl": "en", "gl": "US"}
        client.update(extra)
        cells = []
        st = "?"
        for _, vid in SAMPLES:
            payload = {"context": {"client": client}, "videoId": vid,
                       "contentCheckOk": True, "racyCheckOk": True}
            b, e, _ = http(url, timeout=25, data=json.dumps(payload).encode(),
                           headers={"Content-Type": "application/json"})
            if not b:
                cells.append("网络失败")
                continue
            try:
                d = json.loads(b.decode("utf-8", "replace"))
            except Exception:
                cells.append("非JSON")
                continue
            st = status_of(d)
            cells.append(date_of(d) or ("无日期(%s)" % st))
        say("| %s | %s | %s | %s |" % (name, ver, st, ", ".join(cells)))


# ------------------------------------------------- 3. 第三方免 key 接口
def probe_thirdparty():
    say("\n## 3. 第三方免 key 接口\n")
    say("| 来源 | 可达 | 关键结果 |")
    say("|---|---|---|")
    ids = ",".join(v for _, v in SAMPLES)

    # 3.1 Piped
    for host in PIPED:
        base = "https://" + host
        b, e, t = http(base + "/healthcheck", timeout=12)
        if not b:
            say("| Piped `%s` | 否 | %s |" % (host, e))
            continue
        b2, e2, t2 = http(base + "/channel/" + UCID, timeout=30)
        if not b2:
            say("| Piped `%s` | 是 | 频道接口失败: %s |" % (host, e2))
            continue
        try:
            d = json.loads(b2.decode("utf-8", "replace"))
        except Exception:
            say("| Piped `%s` | 是 | 非 JSON |" % host)
            continue
        rs = d.get("relatedStreams") or []
        withd = sum(1 for x in rs if isinstance(x.get("uploadDate"), (int, float)))
        say("| Piped `%s` | 是 | 一页 %d 条，含 uploadDate %d 条；nextpage=%s |"
            % (host, len(rs), withd, "有" if d.get("nextpage") else "无"))

    # 3.2 Invidious
    for host in INVIDIOUS:
        base = "https://" + host
        u = "%s/api/v1/channels/%s/videos?page=1" % (base, UCID)
        b, e, t = http(u, timeout=25)
        if not b:
            say("| Invidious `%s` | 否 | %s |" % (host, e))
            continue
        try:
            d = json.loads(b.decode("utf-8", "replace"))
        except Exception:
            say("| Invidious `%s` | 是 | 非 JSON |" % host)
            continue
        vids = d.get("videos") or []
        withp = sum(1 for x in vids if isinstance(x.get("published"), (int, float)))
        say("| Invidious `%s` | 是 | 一页 %d 条，含 published %d 条 |"
            % (host, len(vids), withp))

    # 3.3 lemnoslife noKey
    for path in ("noKey/videos", "videos"):
        u = "https://yt.lemnoslife.com/%s?part=snippet,contentDetails&id=%s" % (path, ids)
        b, e, _ = http(u, timeout=25)
        if not b:
            say("| lemnoslife `%s` | 否 | %s |" % (path, e))
            continue
        try:
            d = json.loads(b.decode("utf-8", "replace"))
        except Exception:
            say("| lemnoslife `%s` | 是 | 非 JSON |" % path)
            continue
        items = d.get("items") or []
        pubs = [(it.get("snippet") or {}).get("publishedAt") for it in items]
        say("| lemnoslife `%s` | 是 | items=%d, publishedAt=%s |"
            % (path, len(items), [p for p in pubs if p] or "无"))

    # 3.4 YouTube Data API（无 key 时的报错形态，便于确认是否需要 key）
    u = ("https://www.googleapis.com/youtube/v3/videos?part=snippet&id=%s" % ids)
    b, e, _ = http(u, timeout=20)
    say("| Data API v3（无 key） | %s | %s |"
        % ("是" if b else "否", (b[:160].decode("utf-8", "replace") if b else e)))


# ------------------------------------------------- 4. watch 页 HTML 直取
def probe_watch_html():
    say("\n## 4. watch 页 HTML 直取日期元数据\n")
    say("| 样本 | 可达 | datePublished | uploadDate |")
    say("|---|---|---|---|")
    for tag, vid in SAMPLES:
        b, e, _ = http("https://www.youtube.com/watch?v=" + vid, timeout=25)
        if not b:
            say("| %s %s | 否 | %s | — |" % (tag, vid, e))
            continue
        dp = re.search(rb'"datePublished"\s*:\s*"([^"]+)"', b) or \
            re.search(rb'itemprop="datePublished"\s+content="([^"]+)"', b)
        ud = re.search(rb'"uploadDate"\s*:\s*"([^"]+)"', b)
        say("| %s %s | 是 | %s | %s |" % (
            tag, vid,
            dp.group(1).decode() if dp else "无",
            ud.group(1).decode() if ud else "无"))


# ------------------------------------------------- 5. Wayback
def probe_wayback():
    say("\n## 5. Wayback CDX（快照并集兜底）\n")
    u = ("http://web.archive.org/cdx/search/cdx?url=%s&output=json"
         "&fl=timestamp&filter=statuscode:200&collapse=timestamp:6&limit=20"
         % urllib.parse.quote("https://www.youtube.com/channel/%s/videos" % UCID, safe=""))
    b, e, _ = http(u, timeout=40)
    if not b:
        say("- 不可达: %s" % e)
        return
    try:
        rows = json.loads(b.decode("utf-8", "replace"))
        say("- 可达，返回 %d 行快照时间戳（示例 %s）"
            % (len(rows) - 1, [r[0] for r in rows[1:4]]))
    except Exception:
        say("- 可达但非 JSON: %s" % b[:120])


def main():
    say("# 数据源探针报告")
    say("\n- 生成时间(UTC): %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    say("- 频道: NurdRage `%s`，样本视频 %s" % (UCID, ", ".join(v for _, v in SAMPLES)))
    say("- 说明: 本报告在 GitHub Actions 真实节点上生成，用于决定取日期策略。\n")
    for fn in (probe_env, probe_ytdlp, probe_innertube, probe_thirdparty,
               probe_watch_html, probe_wayback):
        try:
            fn()
        except Exception as ex:
            say("\n> **%s 崩溃**: %s\n" % (fn.__name__, str(ex)[:200]))
    say("\n---\n探针结束。")


if __name__ == "__main__":
    main()
