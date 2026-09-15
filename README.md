# yt-archive · YouTube 频道视频清单云端归档

> **一句话**：把频道链接丢进 `channels.txt`，GitHub 每周自动帮你抓出全量视频清单
> （标题 / 链接 / 时长 / 发布时间，从新到旧），你在国内随时点开下载。**免费、免梯子、免维护。**

数据文件永久链接（收藏这个就够，每次跑完全自动更新）：

```
https://cdn.jsdelivr.net/gh/Rhodcesberyllium/yt-archive@main/data/NurdRage_videos.txt
```

（把末尾文件名换成 `NileRed_videos.txt`、`ChemicalForce_videos.txt` 等即可看别的频道。）

---

## 现在归档了哪些频道

`channels.txt` 里每行一个频道，每次运行会把**全部频道**一起刷新：

| 频道 | 频道主页 | 清单文件 |
| --- | --- | --- |
| NurdRage | <https://www.youtube.com/@NurdRage> | `data/NurdRage_videos.txt` |
| NileRed | <https://www.youtube.com/@NileRed> | `data/NileRed_videos.txt` |
| ChemicalForce | <https://www.youtube.com/@ChemicalForce> | `data/ChemicalForce_videos.txt` |
| Chemiolis | <https://www.youtube.com/@Chemiolis> | `data/Chemiolis_videos.txt` |
| Explosions&Fire | <https://www.youtube.com/@ExplosionsAndFire> | `data/Explosions&Fire_videos.txt` |

每个清单文件底部都有一段 **统计** 和 **数据通道明细**，打开就能看到
「可靠日期（精确到秒或日）占比」是多少。

### 日期会一轮比一轮准

抓取按可信度分多级尝试，同一条视频取最可信的那个来源，并**把已确认的官方日期存进缓存**
（`data/<频道>_dates.txt`，随仓库提交）。所以：

- 某一轮被限流、某个来源暂时不可用 ⇒ 清单**不会**退化，用缓存里的结果
- 每轮都会把还没拿到的视频再补一批，**跑得越多越准**；新视频则每轮都能立刻拿到精确日期
- 想更快补齐：在 Actions 页多点几次 **Run workflow**（点一次等它跑完再点下一次），
  或把 `.github/workflows/fetch.yml` 里的 `cron` 临时改成 `0 15 * * *`（每天一次），
  补得差不多了再改回每周一次

> 实测数据（同一套脚本连续几轮）：NurdRage 可靠日期 7 条 → 277 条 → **291/291（100%）**；
> NileRed 11 条 → 184 条 → **319/401（80%）**，仍在逐轮上升。

---

## 你要记住的三件事

| 想做的事 | 怎么做 |
| --- | --- |
| **立刻刷新一次** | 仓库页 → **Actions** → 左侧 **fetch-youtube-cloud** → 右侧 **Run workflow** → 绿色按钮。几分钟后 `data/` 里的清单全部更新 |
| **抓一个没在清单里的新频道** | 同上，但在 **channel_url** 表单里粘上那个频道的链接 → 运行。那次会"清单里的全部频道 + 你填的这个"一起抓 |
| **把结果拿走** | 仓库 → `data` 文件夹 → 点开对应 `xxx_videos.txt` → 右上角 **Download**；或者直接用上面的 jsDelivr 永久链接 |

---

## 新增 / 停止一个频道

编辑仓库根目录的 [`channels.txt`](channels.txt)（网页上点铅笔图标就能改）：

```
https://www.youtube.com/@NurdRage/videos
https://www.youtube.com/@你要加的频道/videos
```

- **加频道**：加一行 → Commit。之后每次运行都会一起抓。
- **停掉频道**：把那行删掉或前面加 `#`。
- 链接带不带 `/videos` 后缀都一样。

---

## 数据文件怎么看

每个频道产出这些文件：

| 文件 | 是什么 | 要不要动 |
| --- | --- | --- |
| `data/<频道>_videos.txt` | 给你看的清单：名称 / 链接 / 时长 / 发布时间 | 不用动，每轮覆盖 |
| `data/<频道>_dates.txt` | 日期缓存，记录已确认的官方日期 | **别手动改**，删了会退化成模糊日期 |
| `data/_run_log.txt` | 最近一次运行的完整日志 | 出问题时看它 |
| `data/_rotation.txt` | 频道轮转计数（保证限流配额公平分配） | 不用动 |

清单里每条长这样：

```
【1】视频名称: Dissolving $1000 of Platinum to Make $6000 of Chloroplatinic Acid for Professional Use
    视频链接: https://www.youtube.com/watch?v=JNxQq3KFEM4
    时长: 1:24:32 (1h24m32s)
    发布时间: 2024-12-24 23:44:46 (UTC+8)   [原始: 2024-12-24 15:44:46 UTC]
```

发布时间有几种写法，**每种都如实标注精度，绝不假装精确**：

| 写法 | 含义 |
| --- | --- |
| `2024-12-24 23:44:46 (UTC+8)` | 精确到秒（官方 RSS） |
| `2024-12-24 23:44:46 (UTC+8)（精确到秒, 来自X镜像）` | 精确到秒（官方数据接口 / 镜像） |
| `2019-03-27（日精度, 来自watch 页元数据）` | 精确到日（视频页给搜索引擎看的官方日期） |
| `2019-03-27（日精度, 来自archive.org 存档）` | 精确到日（历史快照里存档的官方日期） |
| `2019-03-27（推断日期, 来自标题中的日期）` | 标题里自带的日期，推断 |
| `2025年（推断年份, ±1年, 页面相对文本）` | 只拿到相对时间，只能到年 |
| `未知（…；按视频 ID 时序推算大约在 2016-04 前后（±30 天）；下周自动重跑会再试）` | 所有来源都没拿到，给出区间提示，下轮自动再试 |

> 脚本**从不明知精度不足却写精确时间**。`verify_output.py` 会专门检查这一点，
> 出现伪精度日期会直接让本次运行失败。

文件底部还有：

- **统计**：总数、各精度条数、可靠日期占比、重复标题组
- **数据通道明细**：每条来源各贡献了多少条 + 各阶段耗时（排障用）
- **已从频道消失的视频**（如果有）：上次在、这次不在的，方便你发现被删/转私享的视频

---

## 可选：让日期全部精确到秒（一次配置，5 分钟）

默认走**免 key** 路线（见下一节），好处是什么都不用配；如果要**每一条都精确到秒**，
配一个免费的官方 API key 即可，脚本会自动优先走这条路：

1. 打开 <https://console.cloud.google.com/> → 建一个项目
2. **APIs & Services → Library** → 搜索 **YouTube Data API v3** → **Enable**
3. **APIs & Services → Credentials** → **Create credentials → API key** → 复制那串 key
4. 回到仓库 → **Settings → Secrets and variables → Actions → Secrets → New repository secret**
   - Name 填：`YT_API_KEY`
   - Secret 填：刚才复制的 key
5. 去 Actions 页 **Run workflow** 跑一次

配额说明：`videos.list` 每 50 个视频算 1 个单位，每天免费 10000 个单位。
就算你有几千个视频，一次全量抓取也只用掉几十个单位，**完全在免费额度内，不会产生费用**。

不想用了就把那个 Secret 删掉，脚本自动退回免 key 路线。

---

## 自动更新

- 默认**每周一 10:00 UTC（北京 18:00）** 自动跑一次，把所有频道刷新一遍。
- 也可以在 Actions 页随时手动 `Run workflow`。
- 想改频率：编辑 `.github/workflows/fetch.yml` 里的 `cron`（`0 10 * * 1` = 每周一）。

---

## 它是怎么工作的

墙的问题靠"把活搬到墙外"解决：抓取跑在 GitHub 的美国服务器上，直连 YouTube 无阻碍；
结果作为普通文本文件提交回仓库，你在国内用 GitHub 或 jsDelivr 都能直接看。

日期不是靠猜的，而是按可信度**分级解析**，同一视频取最可信的那个：

```
1. 官方 RSS（精确到秒，最近约 15 条）
2. 官方 Data API v3（配了 YT_API_KEY 就是全量精确到秒）
3. watch 页元数据（读视频页里给搜索引擎看的官方发布日期）← 免 key 路线中实测最有效
4. archive.org 历史 watch 页快照（存档里 YouTube 自己的 uploadDate）
5. Piped / Invidious 公开镜像、YouTube 播放接口、yt-dlp 客户端
6. 标题里自带的日期（推断）
7. 频道页相对时间（"3 years ago"，模糊但保底）
8. 未知 —— 下轮自动重试
```

实测踩到的两个坑，脚本已经按实测结论处理：

- **GitHub 的机房 IP 会被 YouTube 整体风控**：单视频播放接口的 7 种客户端实测全部
  LOGIN_REQUIRED / 网络失败，yt-dlp 的各种 `player_client` 也全为 0；Piped / Invidious
  的公开实例从该 IP 基本连不上。这些通道探测到不可用后会自动进入冷却期（记在缓存里），
  不再每轮白白浪费时间。
- **watch 页通路可用但有总量配额**：连续请求约 200 次后当天不再返回日期。
  所以脚本每轮每频道限 150 条、**每轮把频道顺序轮转一格**，
  让配额逐轮公平分配；配合日期缓存逐轮累积，最终补齐。

---

## 常见问题

**Q1 结果怎么没更新？**
去 Actions 页看最新一次运行。绿色 ✓ 就是成功；红叉点开看哪一步报错，再看 `data/_run_log.txt`
（它随仓库提交，里面有每一步的详细进度和失败原因）。绝大多数是 YouTube 临时限流，
下轮自动重试，也可以直接再 Run 一次。

**Q2 为什么有些视频日期只有年份，或者显示"未知"？**
那个视频在所有公开来源上都没给出确切日期。工具会如实标成"±1年"或"未知"并给出推算区间，
而不是编一个日期。它会后续每轮继续尝试补齐；配上 `YT_API_KEY` 则一次全部变成精确到秒。

**Q3 两个视频标题一模一样，是出错了吗？**
正常。几种情况：① 同一实验发了"直播 + 剪辑/重传"两条；② 视频发布后作者改过名，
工具以频道现行标题为准。工具**按视频 ID 分条**，两个 ID 就是两条真视频。文件底部有"重复标题组"统计。

**Q4 有的视频消失了？**
文件底部会列出来（上次在、这次不在）。这不是工具删的，是频道那边删除/转私享/下架了——
这正是"归档"的意义：记录它曾经存在过。

**Q5 要花钱吗？**
不要。公开仓库的 GitHub Actions 不限时长；数据都是公开信息；Data API 免费额度远超需求。

**Q6 会触发 YouTube 风控吗？**
按频道低频、纯公开信息访问，且已做时间预算、节流与失败退避。即使某轮被限流也不影响已有数据——
缓存会保住之前的结果，下轮自动补。

**Q7 我在跑的过程中改了仓库文件，会不会出问题？**
尽量不要在运行期间改。为避免冲突，最好等一次运行跑完再改文件或点下一次 Run workflow。

---

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `channels.txt` | **频道清单**，你要改的就是这个 |
| `fetch.py` | 云端抓取主程序（枚举 → 多源日期解析 → 归档比对 → 写清单与缓存） |
| `verify_output.py` | 验收自检：格式、去重、条数、排序、伪精度日期，不过就报错 |
| `.github/workflows/fetch.yml` | 定时任务：每周一自动跑 + 支持手动触发和表单填频道 |
| `data/<频道>_videos.txt` | 抓取结果（人读） |
| `data/<频道>_dates.txt` | 日期缓存（机器用，别手动改） |
| `data/_run_log.txt` | 最近一次运行日志（排障用） |
| `部署说明.md` | 从零部署这套东西的完整图文步骤（初次搭建时才需要看） |

本地还有一份**使用手册**：`旧数据/工具脚本/YouTube抓取器/使用手册.md`。
