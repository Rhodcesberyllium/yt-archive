# -*- coding: utf-8 -*-
import json, re, os, glob

def walk_lockups(o, out):
    """收集 JSON 里所有 lockupViewModel 的 (contentId, title, ago)。"""
    if isinstance(o, dict):
        lv = o.get("lockupViewModel")
        if isinstance(lv, dict):
            cid = lv.get("contentId") or ""
            md = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
            t = (md.get("title", {}) or {}).get("content", "")
            ago = ""
            rows = ((md.get("metadata", {}) or {}).get("contentMetadataViewModel", {})
                    or {}).get("metadataRows", []) or []
            for row in rows:
                for p in row.get("metadataParts", []) or []:
                    s = (p.get("text", {}) or {}).get("content", "")
                    if "ago" in s.lower():
                        ago = s
            if cid:
                out.append((cid, t, ago))
        for v in o.values():
            if isinstance(v, (dict, list)):
                walk_lockups(v, out)
    elif isinstance(o, list):
        for v in o:
            walk_lockups(v, out)

def load_json_from_html(path):
    html = open(path, "rb").read()
    html = re.sub(rb"\\x22", b'"', html)
    m = re.search(rb"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S) or \
        re.search(rb"ytInitialData\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1).decode("utf-8", "replace"))
    except Exception:
        return None

base = r"D:\Wu\Test Project\cache\UCIgKGGJkt1MrNmhq3vRibYA\pages"
targets = {"JNxQq3KFEM4": [], "NgXSCjjtZvU": []}
seen_make_glow = []
order = []
for f in sorted(glob.glob(os.path.join(base, "*.html"))):
    if "videos_grid" in f:
        continue  # 旧式 microdata 页，无 lockup
    name = os.path.basename(f)
    data = load_json_from_html(f)
    if not data:
        print(name, "无 ytInitialData")
        continue
    triples = []
    walk_lockups(data, triples)
    print("\n==== %s 共 %d 个 lockup ====" % (name, len(triples)))
    for cid, t, ago in triples[:6]:
        print("   ", cid, "|", t[:52], "|", ago)
    # 目标 id 与关键词
    for cid, t, ago in triples:
        if cid in targets:
            targets[cid].append((name, t, ago))
        if "Make Glow" in t or "Glow in the Dark" in t:
            seen_make_glow.append((name, cid, t, ago))
        if name not in order and cid:
            order.append(name)

print("\n== 目标 ID 各来源标题 ==")
for cid, lst in targets.items():
    print(cid, "->", lst if lst else "（本缓存中未见）")
print("\n== 含 Make Glow 的条目 ==")
for row in seen_make_glow:
    print("   ", row)
