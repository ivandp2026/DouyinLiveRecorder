# Build 19 测试说明

本测试版在 Build 18 的斗鱼 Streamlink 录制与自动续录基础上，增加完整弹幕数据、热度分析和弹幕版视频。

## 测试方式

1. 解压 ZIP，运行 `DouyinLiveRecorder.exe`。
2. 确认 `config/config.ini` 中：
   - `是否开启斗鱼弹幕分析(是/否) = 是`
3. 在 URL 配置中加入斗鱼直播间，例如：
   - `https://www.douyu.com/5135383`
4. 录制数分钟后停止该直播间或退出程序。
5. 等待控制台中的弹幕视频渲染完成。长视频渲染时间会明显长于数据报告生成时间。

## 每场直播应产生

假设原视频为 `主播_时间.ts` 或 `主播_时间.mp4`，同目录应出现：

- `主播_时间.danmaku.mp4`：带当时弹幕的 MP4
- `主播_时间.danmaku.raw.jsonl`：全部原始弹幕/礼物/系统事件
- `主播_时间.danmaku.timeline.csv`：逐秒弹幕与热度数据
- `主播_时间.danmaku.keywords.json`：关键词与爆发时间
- `主播_时间.highlights.json`：热点区间及完整分析数据
- `主播_时间.danmaku.report.html`：可直接打开的热度报告
- `主播_时间.danmaku.ass`：用于重新烧录的 ASS 弹幕轨
- `主播_时间.danmaku.render.json`：弹幕视频渲染状态
- `主播_时间.danmaku.txt`：便于人工查看的时间轴文本

原始录制文件不会被覆盖或删除。

## 热度字段

`danmaku.timeline.csv` 包含：

- `second`
- `raw_comment_count`
- `comment_count`
- `duplicate_count`
- `unique_users`
- `gift_count`
- `like_count`
- `repeat_ratio`
- `heat_score`

原始数据全部保留；重复刷屏只会降低分析权重和弹幕视频显示密度。

## 首轮测试重点

- 斗鱼链接能否正常识别并开始录制
- 原视频是否保持不变
- JSONL 是否持续写入
- 停止录制后报告是否立即生成
- `danmaku.render.json` 最终是否变成 `completed`
- `danmaku.mp4` 的弹幕时间是否与画面基本同步
