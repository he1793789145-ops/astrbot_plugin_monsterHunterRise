# 怪物猎人素材查询（AstrBot 插件）

在群聊里查询《怪物猎人 崛起：曙光》素材的获取方式，以图片卡片回复。

面向 OneBot v11（aiocqhttp）平台，已在 AstrBot 4.x 上开发。

## 指令

| 指令 | 说明 |
|---|---|
| `/mh 素材 <素材名>` | 查素材：哪只怪物、哪个部位/方式、概率、数量，以及哪些任务可刷 |
| `/mh 怪物 <怪物名>` | 查某只怪物的完整掉落表（按下位/上位/大师分组） |
| `/mh 任务 <任务名>` | 查任务的星级、地图、出场怪物与结算奖励 |
| `/mh help` | 显示全部指令 |

不带参数、或子命令拼错时会自动回帮助。

## 安装

1. 把整个 `astrbot_plugin_mh_material` 目录放进 AstrBot 的插件目录：
   `<AstrBot>/data/plugins/astrbot_plugin_mh_material`
2. 装入依赖（AstrBot 主环境通常已带 Pillow ≥ 10）：
   ```
   pip install -r requirements.txt
   ```
3. 在 AstrBot 面板重载插件。

## 数据

插件不联网，只读一份本地快照，因此查询是纯内存操作、响应快且不受外部服务影响。

### 快照位置

按以下顺序查找，先命中先赢：

1. 环境变量 `MH_MATERIAL_SNAPSHOT` 指定的路径
2. `<AstrBot>/data/plugin_data/mh_material/snapshot.json` — **正式部署位置**
3. 插件自带的 `data/plugin_data/mh_material/snapshot.json`

### 重新生成快照

**用 `tools/rebuild_snapshot.py`，不要单独跑 `extract.py`。**

快照由三步依次叠加，顺序不能乱：

| 步骤 | 脚本 | 写入 |
|---|---|---|
| 1 | `extract.py` | 基础表（**会重写整个快照**） |
| 2 | `fetch_gathering.py` | `items[].gathering`（采集点，Kiranico） |
| 3 | `fetch_habitats.py` | `monsters[].map_names`（栖息地图，gamecat） |

```
python tools/rebuild_snapshot.py
python tools/rebuild_snapshot.py --out "<AstrBot>/data/plugin_data/mh_material/snapshot.json"
python tools/rebuild_snapshot.py --skip-fetch    # 只跑第 1 步，离线可用
```

脚本跑完会**校验各数据块是否都在**（采集点 ≥40、栖息地图 ≥90、部位名 ≥500），
不足会明确报出来。

> ⚠️ **踩过的坑**：`extract.py` 会重写整个快照。如果只跑它 + 第 3 步而漏掉第 2 步，
> 采集点数据会被静默抹掉——**没有任何报错**，卡片上只是安静地少了一节
> （「散发土香的重泥骨」这类只能挖骨头堆获得的素材就会显示成「没有获取途径」）。
> 这正是 `rebuild_snapshot.py` 存在的原因。

单独跑 `extract.py` 时的可选参数：

```
--mhrice <路径>     指定 mhrice.json
--items <路径>      指定 items.json
--pretty            缩进输出，便于人工检查
```

### 源数据

`tools/extract.py` 从这两个文件提取：

- `mhrice.json`（约 118 MB）— 游戏文件转储，提供掉落表、任务表、地图与中文名
- `items.json` — 物品库，提供素材中文名（简中）

采集点数据由 `tools/fetch_gathering.py` 单独抓取（见下），
`armor.json` / `equip_skills.json` / `normal_decos.json` 目前**不使用**（属于配装数据），
详见下面的「扩展」。

### 采集点数据（来自 Kiranico）

野外采集类素材（矿石、骨头、木桶等 47 种）的「地图 + 难度 + 概率」来自
[mhrise.kiranico.com](https://mhrise.kiranico.com/zh/data/items?view=material)。
由 `rebuild_snapshot.py` 的第 2 步调用：

```
python tools/rebuild_snapshot.py                # 推荐：三步一起跑
python tools/fetch_gathering.py --probe         # 只探测几个素材，验证页面结构
```

脚本会并发抓取素材页（并发 3、每请求间隔 0.4s），原始结果缓存到
`gathering_cache.json`，然后只把「地图 / 难度 / 数量 / 概率」这类事实字段并入快照。

**注意**：该站点**没有许可证声明**，所以脚本只提取事实数据、不复制任何文案，
也不做整站镜像。是否使用请自行判断；不用的话删掉快照里的 `gathering` 字段即可，
插件会正常跳过该小节。

实测覆盖：1199 个素材页全部抓取成功、0 失败，其中 47 个有采集点数据——
其余素材是怪物掉落或任务报酬获取，本来就没有采集点。

### 怪物栖息地图（来自 gamecat.fun）

「这只怪/小动物去哪张图找」来自 [gamecat.fun（游猫网）](https://gamecat.fun/rise/zh/)。
由 `rebuild_snapshot.py` 的第 3 步调用：

```
python tools/rebuild_snapshot.py                # 推荐：三步一起跑
python tools/fetch_habitats.py --probe          # 只验证解析
```

用 MediaWiki API 批量取怪物页「怪物简介」段的 `出现场地：` 字段（112 只怪物分 3 批，
每批 ≤50 个标题），只提取「怪物 → 地图名」映射，缓存到 `habitat_cache.json`。
实测 105 只怪物有地图字段，写入快照 102 只。

**为什么需要单独抓**：`mhrice.json` 里**小动物的地图数据是空的**
（`small_monsters[].habitat_area` 恒为 `null`，
`ecological.stage_info_list` 实测 37 只里只有 2 条有值），所以无法从源数据得到。

**同样未声明许可证**，处理方式与采集点一致：只取事实字段、标注来源。

## 数据可信度说明（重要）

这份快照有三处已知的局限，都是**源数据本身**的限制，不是插件 bug。使用前请知悉：

### 1. 地图 12 / 13 的名称是推断值

源数据的 `map_name` 表只有官方编号 1~11 的名字，但大师任务里用到了 `12` 和 `13`。
插件按「任务出场怪物」推断为 **12 = 密林、13 = 城塞高地**。

**已获独立印证**：Kiranico 的采集点数据里直接出现「密林」「城塞高地」两张地图
（例如「有苔藓的重土骨」只在密林、「受损的重古骨」只在城塞高地），与推断一致。
不过源数据里仍没有 `12 → 密林` 的直接对照，快照里保留 `"origin": "inferred"` 标注，
卡片底部也会显示「地图 … 为推断值」。

### 2. 怪物名映射：已修正，但源数据本身有缺陷

`mhrice.json` 里怪物名表与怪物数据之间**没有可靠的关联字段**。曾经用过的三种做法都错：

| 做法 | 命中率（以掉落物名为基准） |
|---|---|
| 数组下标对齐 | 2/52 ❌ |
| `monsters[i].id` 对齐 | 1/52 ❌ |
| `enemy_type` 直接索引 | 37/52 |
| **掉落物名前缀 + 显式编号映射**（现行） | **52/52 基础种，78/78 无重名** ✅ |

现行做法是两条证据合成：

1. **掉落物名前缀**（首选）：掉落素材名形如「爆鳞龙的爆腺」，取「」前的名字；
   同一前缀出现 ≥2 次且是名字表已知怪物名才采信。这是游戏自带的一手数据。
2. **显式编号映射**：基础区间 `enemy_type` 直接查 `monster_names`；
   大师区间用 `MR_INDEX_BY_ENEMY_TYPE` 逐条列举（该区间偏移不规则，公式不成立）。

两者冲突时以掉落物为准；两者同族时取更具体的那个，以保住变体名
（`红莲爆鳞龙`、`怪异克服钢龙`、`霸主・火龙` 等）。

**已知不确定项**：`em=131`（推出为「精灵鹿」）在源数据里没有掉落记录，
无法用掉落物校验，可能与游戏内名称有出入。

### 2b. 小动物名映射：曾经整体错位一位

小动物（精灵鹿、丸鸟、野猪…）用独立的 `Ems` id 空间，名字同样要合成。曾经的做法是
**按名字表位置逐条硬写一张覆盖表**，结果**整体错位一位**，37 只里 24 只贴错：

| 现象 | 真实来源 |
|---|---|
| 「药草」被标成**毒狗龙**掉落 | 其实是**丸鸟** |
| 「温暖的毛皮」被标成别人的 | 其实是**精灵鹿** |

现行判定顺序（`build_monsters` 里的小动物分支）：

1. **掉落物前缀强签名**（首选）——小动物掉自己的素材（「丸鸟的羽」→ 丸鸟），
   同一前缀出现 ≥2 次才采信。这是游戏一手数据，实测 37 只里 30 只可判定。
2. **`SMALL_NAME_OVERRIDES` 人工表**——必须排在编号兜底**之前**，否则编号会先
   命中一个错误的怪名（实测 `Ems=3` 的编号位置是「爆鳞龙」，而它其实是精灵鹿）。
3. **编号兜底**——名字表位置 = `enemy_type - 2`，该偏移对已验证条目成立，
   但本文件的编号体系不统一，没有通用公式。
4. **弱签名**（最后）——掉落物里出现一次的已知怪名。放最后是因为它容易抽到
   「巨大」「优质」这类通用词对应的怪，误判率高。

剩余无法用掉落物验证的 5 只（艾露猫/梅拉露、波波、砂鱼、变形幼冰鲨）已逐只人工核对：
波波→「波波舌」、变形幼冰鲨→「鲛肌的鳞」。**艾露猫与梅拉露的掉落完全相同**
（肉球印章、肉球优待券），只能按名字表顺序区分，属推断。

### 2c. 别名（日文名）已全部停用

`monster_aliases` 表与名字表**同样整体错位一位**：别名表下标 0 是「雌火竜」，
对得上名字表下标 0 的「雌火龙」，但 `Alias_EnemyIndex001` 标的是
「雌火竜 ヌシ・リオレイア」，按编号取会取到下一只怪的别名。

逐条校验（把「竜→龙」归一后比对汉字）后，78 只里只剩极少数能自洽，
且仍有「霸主・火龙」被当成「火龙」的错例。别名在卡片上只是装饰，
**错的名字比没有更糟**，所以现在直接不输出。

### 2d. 掉落物里的非素材类物品

小动物会掉结算道具和消耗品（丸鸟蛋 `CarryPayOff`、生肉、药草、怪物体液）。
早先快照只收录 `Material` / `OffcutsMaterial` 类型，这些物品被过滤掉后，
掉落记录指向一个**不存在的 id**，卡片上就出现空白的素材名
（「掉落物 1个 100%」后面没字）。

现在**所有被掉落记录引用到的物品都会进表**（快照 1349 → 1384 项）。
校验：6475 条掉落记录里解析不到物品名的为 **0 条**。

### 3. 部位破坏的部位名（部分可判定）

`mhrice.json` 能给出「部位破坏报酬」的概率，但**部位名不在同一张表里**：

* 部位身份在 `monster_lot.parts_break_list` 的 `RandomId`；
* 名字在 `monsters[].collider_mapping.part_map`（游戏内日文，如 `頭部`/`尻尾`），
  按 `RandomId` 取键（实测 40 只怪里 32 只完全吻合）；
* 奖品数组是「部位组 × 每组若干项」的扁平数组，但**布局因怪而异**：
  爵银龙 60 项 / 3 组 = 每组 20，伞鸟 60 项 / 4 组但间隔是 10。

所以 `part_group_map()` 会把候选切法都试一遍，取**能覆盖最多部位组**的那个；
一个都覆盖不全时不猜（宁可只说「部位破坏报酬」，也不给错部位）。

实测：1027 条部位破坏记录里 **616 条（59%）能标出部位名**，分布为
头部 184、尾巴 59、左脚 40、躯干 39、右翼 38……
`ダメージアタリ`、`Group`、纯数字这类内部标记会被过滤掉，不会混进卡片。

## 与最初需求的两点差异

开发时按实测数据做了调整，这里明确记录，避免误解：

1. **任务信息分两块呈现，来源不同。**
   - **可刷任务**：通过「哪些任务会出场掉落该素材的怪物」推导，标注了具体打哪只怪。
     这是怪物素材（如龙玉、天鳞）获得任务信息的主要途径。
   - **任务报酬**：任务的结算奖励表（`quest_data_for_reward`）里直接产出的素材，
     例如「妖辉石」完全没有怪物掉落来源，只在任务 315100 / 315108 的结算奖励里。
     只覆盖一部分素材（本快照 978 种素材中有 181 种是**仅任务报酬**来源），有才显示。
2. **活动任务与常驻任务混排**，活动任务带【活动】前缀标注（活动任务是限时轮换的）。

## 疑难排查

**查询没反应 / 回了「用法：/mh ...」**
说明参数没传进 handler。本插件**不依赖** `@filter.command` 的注解式参数解析
（AstrBot 的 `CommandFilter.init_handler_md` 直接读 `inspect.signature` 的注解，
而 `from __future__ import annotations` 会把注解变成字符串，导致解析失效），
改为在 handler 里解析消息文本。如果你改动 `main.py`，请保持这个方式。

**「没有找到名为 XXX 的素材」**
先确认名字写法（用 `/mh 素材 <名字的一部分>` 试模糊匹配，会列出候选）。
若确实不在其中，说明该素材不在本快照覆盖范围内。

**素材查到了但显示「暂无掉落记录」**
这是数据事实，不是缺失：本插件的快照包含全部 1349 个素材/余料类物品，
其中约 570 个**没有**怪物掉落或任务报酬记录——它们来自野外采集、交易，
或源数据（`mhrice.json`）本身没有收录掉落表。当前版本不查询采集点。

## 自测

不依赖 AstrBot 运行时即可验证数据层与渲染层：

```
python tools/selftest.py            # 生成卡片到 _selftest_out/
python tools/selftest.py --no-png   # 只跑逻辑
```

建议直接用 AstrBot 自带的解释器跑，能同时确认 Pillow 等依赖在位：

```
<AstrBot>/venv/Scripts/python.exe tools/selftest.py
```

另有一个针对 AstrBot 集成的回归测试（加载 AstrBot 真实源码模块，验证指令参数解析）：

```
python tools/verify_astrbot_args.py --astrbot-dir "<AstrBot 源码目录>"
```

它检查：handler 签名不依赖注解、真实 `CommandFilter` 能匹配指令、
`extract_argument` 的各种消息形态、以及端到端 dispatch。
改完 `main.py` 的指令处理建议跑一次。

## 导入约定（修改代码时注意）

AstrBot 是把插件目录当作**包**加载的，因此插件内部的模块必须用**相对导入**：

```python
# 正确
from .render import Card
from . import commands as cmd

# 错误 —— 会报 No module named 'render'
from render import Card
```

`tools/` 下的脚本不受此约束（它们不被 AstrBot 加载），
但 `tools/selftest.py` 仍按包导入插件，以保证测的就是实际部署的导入方式。

## 扩展：配装 / 技能查询

命令表与数据层都为后续扩展留了位置：

- `commands.py` 的 `COMMANDS` 列表是命令的单一事实源，`/mh help` 由它生成。
  加 `/mh 配装` 只需追加一项，不必改命令解析或帮助文本。
- `data.py` 按需加载表：快照里没有的表会被跳过，因此旧快照不会因为新表缺席而报错。
- `tools/extract.py` 末尾有 `extract_armor()` / `extract_skills()` 的占位说明，
  源数据 `armor.json`、`equip_skills.json`、`normal_decos.json` 已经具备。
- `render.py` 的 `Card` / `Section` / `Line` 是通用的卡片原语，配装卡片可直接复用。

## 渲染后端

默认用 PIL 本地绘制，**不依赖 AstrBot 的文转图服务**（该服务默认关闭）。

如果你在 AstrBot 面板里配好了 t2i，可以切到 HTML 后端走官方服务：

```
set MH_MATERIAL_RENDER=html
```

渲染失败时会自动回退成纯文本，用户始终能看到内容。

## 文件说明

```
astrbot_plugin_mh_material/
├── main.py          插件入口：指令注册、快照加载、结果发送
├── commands.py      子命令注册表、卡片构建、帮助生成
├── data.py          快照加载、索引与查询
├── render.py        卡片渲染（PIL / HTML 双后端）
├── metadata.yaml    插件元数据
├── requirements.txt 运行期依赖
├── tools/
│   ├── extract.py           离线提取：源数据 → 紧凑快照
│   ├── fetch_gathering.py   抓取 Kiranico 采集点并并入快照
│   ├── selftest.py          离线自测
│   └── verify_astrbot_args.py  AstrBot 指令解析回归测试
├── data/plugin_data/mh_material/
│   ├── snapshot.json        快照
│   └── gathering_cache.json 采集点抓取的原始缓存
```
