# Bilibili Creator Demo Help

本文档说明 `bili_creator_demo.py` 修改后的批量 UP 主投稿爬取逻辑。

## 一、主要能力

脚本现在支持：

- 单个 UID、多个 UID、或 JSON 文件中的 UID 列表。
- 使用 `launch_persistent_context` 复用本地浏览器登录态。
- 分页获取每个 UID 的投稿列表。
- 输出普通 JSON 文件，不再追加写入 jsonl。
- 每条视频保留核心字段，并新增 `is_union_video` 表示是否为合作视频。
- 每页成功后立刻原子写入结果，重跑时默认按已有页和视频去重续爬。
- API 请求失败时按指数退避重试，并在重试前刷新浏览器 cookie 和 WBI key。

## 二、运行方式

单个 UID：

```bash
uv run python bili_creator_demo.py --creator 434377496 --max-pages 2
```

多个 UID：

```bash
uv run python bili_creator_demo.py --creators 434377496,23972272 --max-pages 2
```

也可以重复传 `--creator`：

```bash
uv run python bili_creator_demo.py --creator 434377496 --creator 23972272
```

从 JSON 文件读取 UID：

```bash
uv run python bili_creator_demo.py --uid-json uids.json --output output/bili_creator_videos.json
```

`uids.json` 支持：

```json
["434377496", "23972272"]
```

或：

```json
{"uids": ["434377496", "23972272"]}
```

## 三、输出结构

默认输出到：

```text
bili_creator_videos.json
```

顶层结构：

```json
{
  "generated_at": "2026-07-24T12:00:00Z",
  "updated_at": "2026-07-24T12:01:00Z",
  "config": {
    "max_pages": 2,
    "page_size": 40,
    "sleep_seconds": 1.0,
    "retries": 3,
    "retry_delay": 2.0,
    "request_timeout": 30.0
  },
  "creators": [
    {
      "uid": "434377496",
      "started_at": "2026-07-24T12:00:00Z",
      "finished_at": "2026-07-24T12:01:00Z",
      "status": "success",
      "total": 80,
      "pages_crawled": [1, 2],
      "videos": []
    }
  ]
}
```

每个 `videos` 条目的核心字段：

```json
{
  "aid": 116970011824178,
  "bvid": "BV1nwgx6PEZu",
  "title": "视频标题",
  "created": 1784820893,
  "length": "03:35",
  "play": 182035,
  "comment": 2985,
  "description": "",
  "is_union_video": false,
  "url": "https://www.bilibili.com/video/BV1nwgx6PEZu"
}
```

其中 `is_union_video` 来自接口返回的同名字段，脚本会转换为布尔值。

## 四、登录态

脚本只保留 `launch_persistent_context` 这一条主要登录链路。默认 profile 路径为：

```text
browser_data/bili_creator_demo_user_data
```

第一次运行时，如果没有登录态，会打开浏览器并等待手动登录。登录完成后，cookie 会保存在这个持久化目录中，后续运行通常可以直接复用。

仍然可以用 `--cookie` 临时注入 cookie：

```bash
uv run python bili_creator_demo.py --creator 434377496 --cookie "SESSDATA=...; bili_jct=..."
```

## 五、重试与恢复

相关参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--retries` | `3` | 每页 API 请求最多尝试次数。 |
| `--retry-delay` | `2.0` | 首次重试等待秒数，后续按指数增加。 |
| `--request-timeout` | `30.0` | httpx 请求超时时间。 |
| `--sleep` | `1.0` | 翻页和 UID 切换之间的等待秒数。 |
| `--no-resume` | 关闭 | 不读取已有输出，从头生成新结果。 |

恢复机制：

- 每抓完一页就写一次 JSON 文件。
- 写文件使用临时文件加 `replace`，避免半写入导致 JSON 损坏。
- 默认启用 resume，重跑时会读取已有 `pages_crawled`，跳过已经成功完成的页。
- 视频合并时按 `bvid` 去重，避免重复运行造成重复数据。
- 请求失败后会等待、刷新 cookie/WBI key，再重试当前页。

## 六、参数说明

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--creator` | 空 | 单个 UID 或空间主页 URL，可重复传入。 |
| `--creators` | 空 | 逗号分隔 UID 或空间主页 URL。 |
| `--uid-json` | 空 | UID 列表 JSON 路径。 |
| `--max-pages` | `1` | 每个 UID 最多抓取页数。 |
| `--page-size` | `40` | 每页视频数量，范围 1 到 50。 |
| `--sleep` | `1.0` | 翻页或切换 UID 的间隔。 |
| `--headless` | `False` | 是否使用无头浏览器。 |
| `--cookie` | 空 | 临时注入 B 站 cookie。 |
| `--user-data-dir` | `browser_data/bili_creator_demo_user_data` | Playwright 持久化 profile 目录。 |
| `--output` | `bili_creator_videos.json` | JSON 输出路径。 |

## 七、保留与删除

保留：

- `bili_creator_demo.py`：批量爬取入口。
- `bili_signin.py`：WBI 签名逻辑。

删除：

- `signin.py`：旧的独立手动登录脚本，已由主脚本中的持久化登录流程覆盖。
- `bili_creator_video.py`：空壳文件，没有有效业务逻辑。
