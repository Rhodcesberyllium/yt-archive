# -*- coding: utf-8 -*-
import re
html = open(r"D:\Wu\Test Project\cache\UCIgKGGJkt1MrNmhq3vRibYA\pages\uu_playlist.html", "rb").read().decode("utf-8", "replace")
idx = html.find("JNxQq3KFEM4")
seg = html[idx - 200: idx + 2500]
# 打印所有疑似时长/徽章 key
for key in ("lengthText", "duration", "overlay", "Badge", "badge", "videoCountText", "textContent", '"text"'):
    c = seg.count(key)
    if c:
        print(key, c)
mm = re.findall(r'"text":"([\d:]{4,10})"', seg)
print("时间样式文本:", mm[:6])
print("---seg 头 500---")
print(seg[:500])
