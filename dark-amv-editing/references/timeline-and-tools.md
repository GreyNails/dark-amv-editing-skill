# 时间数据与随附工具

工具以 Python 3 标准库实现。审查器不需要媒体软件；抽帧工具需要可在 PATH 中调用的 `ffmpeg`、`ffprobe`。它们不依赖本机原工程、显卡、私有目录或在线服务。

## 时间线的最小约定

使用 [assets/timeline.example.json](../assets/timeline.example.json) 作为结构起点。示例是 8 秒、3 个镜头的虚构制作数据，文字为原创占位句；时间不是歌曲的真实鼓点，也不包含可直接渲染的素材。

| 字段 | 含义 |
|---|---|
| `fps` / `frames` | 输出帧率 / 总帧数；例子采用 30fps |
| `sources[]` | 素材 `id`、`path`、声明 `duration`（秒） |
| `shots[].start_frame/end_frame` | 输出绝对帧区间，左闭右开 |
| `source_id/source_in/source_span` | 素材 ID、源入点（秒）、读取的源时长（秒） |
| `ramp` | `[输出归一化位置, 源归一化位置]` 的单调控制点，覆盖 `[0,0]` 至 `[1,1]` |
| `shots[].beat_frames` | 镜头内局部帧，绝对帧 = `start_frame + 局部帧` |
| `lyrics[].start/end` | 全曲显示区间（秒），左闭右开 |
| `lyrics[].shot_id` | 可选，绑定该次文字构图区间的镜头 |
| `lyrics[].beat_frames` | 全曲绝对帧，不与镜头内帧混用 |
| `entry_landing_frame` | 全曲绝对帧：入场动画落定时刻 |
| `enter_duration` | 入场时长（秒），落定约为 `start + enter_duration` |
| `pos` / `quad` | 归一化画布坐标；`quad` 为按边界顺序排列的四角 |

`source_span` 不等于输出镜头长度，速度可变化。不同原始帧率的素材共用输出帧格时，不要把输出帧索引直接当成源帧索引。时间换算后，使用与渲染器一致的舍入和采样规则。

`ramp` 可省略或为 `null`，表示没有显式变速曲线。为读取 V3 数据，审查器还认识两个旧预设：`gather` 对应 `[[0,0],[0.40,0.18],[0.70,0.80],[1,1]]`，`strike` 对应 `[[0,0],[0.22,0.12],[0.58,0.86],[1,1]]`。新工程优先直接写控制点；未知预设或空数组会报错。

完整工程还可保存 `grade`、`crop_native`、`zoom`、`bars`、`geometry`、`colour_masks`、`manga_panels`、`matte` 等字段；这些字段是视觉配方，随附审查器不会执行它们，也不会检查所有特效的完整模式。参考文档中的 V3 参数名不意味着本仓库含有对应渲染器。

## 结构审查

从仓库根目录执行：

```bash
python3 dark-amv-editing/scripts/audit_timeline.py \
  dark-amv-editing/assets/timeline.example.json
```

审查自己的工程并检查视频源文件是否存在：

```bash
python3 dark-amv-editing/scripts/audit_timeline.py work/timeline.json \
  --project-root work --check-files --output work/audit.json
```

相对素材路径按 `--project-root` 解析；省略时按时间线文件所在目录。`--check-files` 只检查 `sources[].path`，不检查音乐、蒙版或漫画资产，也不探测媒体内部格式。示例不提供 `media/`，不应直接对示例启用该项。

源区间复用默认报告 warning；需要严格杜绝复用时追加 `--forbid-reuse`。审查结果包括 `ok`、统计以及有字段定位的 `errors` / `warnings`。退出码：0 没有错误（可有警告），1 存在结构错误，2 输入或运行失败。它不会通过音频确认鼓点，也不会通过图片确认字是否挡脸。

歌词脉冲在显示区间外但距边界不超过一帧时会警告，以便复核跨镜交接；更远的错位会报错。落定晚于文字显示区间也会提示。警告不是“此段文字一定可读”的证明，应按[音乐与空间歌词](music-lyrics-story.md#7-将数据正确与实际可读分开验证)的方法看实际成片。

## 成片抽帧

先完成实际视频渲染，再取指定时间：

```bash
python3 dark-amv-editing/scripts/review_frames.py work/final.mp4 \
  --at 6.5 --at 51.2 --at 114.5 --out work/review-picked
```

按时间线取每镜首、中、末帧以及文字进入、落定、中间和退出的时刻：

```bash
python3 dark-amv-editing/scripts/review_frames.py work/final.mp4 \
  --timeline work/timeline.json --include-lyrics \
  --width 960 --out work/review-all
```

只想看分镜总览时加 `--samples mid`。输出 JPEG、`frames.json` 和可本地打开的 `index.html`；点击图片可看单图。目标目录必须不存在或为空，避免覆盖上次审片证据。视频必须与这份时间线对应，工具不会替你验证渲染缓存签名。

记录的是目标 seek 时间；对 VFR、非零起始 PTS 或边界非常敏感的素材，应另外核对解码 PTS。正常最终交付建议先统一为已验证的 CFR。单帧抽查之外，仍需正常速度观看相邻镜头、最短歌词和转场。

## 验证工具

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖时间数据不变量，以及使用 FFmpeg 生成临时测试视频、实际解码抽帧、输出图库和保留已有目录的行为。没有 FFmpeg 时媒体测试会跳过；要验证完整抽帧流程应安装后重跑。
