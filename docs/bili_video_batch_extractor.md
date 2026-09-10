# Bilibili 作者与视频批量抓取脚本说明

本文说明 [`bili_video_batch_extractor.py`](./bili_video_batch_extractor.py) 的运行方式、并发模型、重试机制、断点续跑规则及输出结构。

## 1. 脚本用途

脚本读取 `video_batches` 目录下的作者—视频分组 JSON，分别抓取：

- 作者结构化信息：`/x/web-interface/card`
- 视频结构化信息：`/x/web-interface/view`

这两个接口不需要 Cookie、浏览器登录态或 WBI 签名。因此该脚本不包含 Playwright、Cookie 刷新、签名计算和播放地址提取逻辑。

任务以“作者”为并发和断点单位：多个作者可同时处理，但一位作者内部的视频按顺序请求。每位作者全部处理完后，主线程将其结果保存为一个独立 JSON 文件。

## 2. 基本运行方法

处理一个完整批次：

```bash
uv run python bili_video_batch_extractor.py \
  --input video_batches/video_batches_0.json
```

指定输出目录和线程数：

```bash
uv run python bili_video_batch_extractor.py \
  --input video_batches/video_batches_0.json \
  --output-dir video_batches_result/video_batches_0 \
  --workers 8
```

第一次运行建议先小规模试跑：

```bash
uv run python bili_video_batch_extractor.py \
  --input video_batches/video_batches_0.json \
  --workers 2 \
  --max-creators 2 \
  --max-videos 3
```

未指定 `--output-dir` 时，默认输出位置为：

```text
video_batches_result/<输入文件名，不含 .json>/
```

例如输入 `video_batches/video_batches_0.json`，默认输出到 `video_batches_result/video_batches_0/`。

## 3. 输入格式

输入 JSON 的根节点必须是数组，每个元素表示一位作者：

```json
[
  {
    "uid": "8542312",
    "videos": [
      "BV1QGgy6uEMy",
      "BV1DdN16FEjq"
    ]
  },
  {
    "uid": "836885",
    "videos": [
      "BV1xxxxxxxxx"
    ]
  }
]
```

启动任务前，`load_tasks()` 会进行以下检查和整理：

1. JSON 根节点必须是数组。
2. 每个作者项必须是对象。
3. `uid` 必须是纯数字，字符串和整数形式都可以。
4. `videos` 必须是数组。
5. 每个视频必须是符合 `BV` 加 10 位字母或数字格式的字符串。
6. 重复 UID 会被合并，作者的所有视频按首次出现顺序组合。
7. 同一作者内的重复 BV 号会被删除。
8. 不同作者之间的重复 BV 号会保留，因为它们可能代表联合投稿关系。

发现无法识别的数据时，脚本会在发起网络请求前直接退出，并指出对应作者或视频的位置。

## 4. 总体执行流程

```text
解析命令行参数
        ↓
读取并校验输入 JSON
        ↓
合并重复 UID、作者内 BV 去重
        ↓
读取已有作者结果，识别断点
        ↓
把未完成作者提交到线程池
        ↓
每个作者：抓作者资料 → 顺序抓取该作者的视频
        ↓
主线程接收完成结果
        ↓
原子写入 creators/<uid>.json
        ↓
更新 manifest.json
        ↓
全部完成或安全处理中断
```

入口函数为 `main()`，主要工作由 `run()` 组织。

## 5. 线程并发模型

脚本使用 `ThreadPoolExecutor`，默认 `--workers 8`。

一个 `CreatorTask` 对应一位作者及其 BV 号列表。所有未完成作者会提交给线程池，但线程池同时最多运行 `workers` 个任务，并不会为每位作者都创建一个线程。

线程分工示意：

```text
线程 1：作者 A → A 的视频 1、2、3……
线程 2：作者 B → B 的视频 1、2、3……
线程 3：作者 C → C 的视频 1、2、3……
...
主线程：接收作者结果并写文件
```

作者内部不再并发请求视频，原因是：

- 避免单个拥有数百个视频的作者瞬间产生大量请求。
- 请求间隔和重试顺序更容易控制。
- 一位作者对应一个完整输出对象，断点边界清晰。

每位作者任务会创建自己的 `httpx.Client`，在处理该作者期间复用 HTTP 连接。作者完成后客户端自动关闭。

只有主线程负责写结果文件，因此不存在多个工作线程同时修改同一个 JSON 的问题。

## 6. 单位作者的处理过程

`crawl_creator()` 按以下顺序处理一位作者：

1. 读取该作者上一次保存的结果。
2. 如果已有成功的作者资料，则直接复用。
3. 如果作者资料缺失或上次失败，则调用 `card` 接口重新抓取。
4. 建立“已成功视频 BV 号 → 视频结果”的索引。
5. 按输入顺序遍历该作者的所有 BV 号。
6. 已成功的视频直接复用，失败或缺失的视频重新请求 `view` 接口。
7. 统计视频成功数与失败数。
8. 生成作者级状态并把完整结果返回主线程。

作者资料接口失败不会阻止视频继续抓取；单个视频失败也不会中断同一作者后面的其他视频。

## 7. 请求间隔、重试和风控

所有请求都通过 `Requester.get()` 发出。

### 7.1 普通请求间隔

同一作者的相邻请求默认间隔 `0.2～0.6` 秒，每次随机取值。这个间隔按作者任务独立计算，而不是所有线程共享一个全局请求间隔。

因此，当 `workers=8` 时，最多可能有 8 位作者同时请求。提高线程数会提高整体请求速率，也会增加触发风控的概率。

### 7.2 可重试错误

以下错误会自动重试：

- HTTP 408
- HTTP 5xx
- 网络连接错误
- 请求超时
- 响应不是合法 JSON
- 响应的 `data` 结构异常
- HTTP 403、412、429 风控类响应
- Bilibili API 业务码 `-352`、`-412`、`-509`

其他明确的 API 业务错误不会反复请求，而是直接记录到结果中。

### 7.3 退避算法

普通重试采用指数退避：

```text
第 1 次失败：retry_delay
第 2 次失败：retry_delay × 2
第 3 次失败：retry_delay × 4
```

实际等待时间还会增加最多 25% 的随机抖动，避免多个线程在同一时刻重新发送请求。

如果响应携带数值形式的 `Retry-After`，实际等待时间不会短于该值。

### 7.4 全局风控冷却

`RiskCooldown` 是所有线程共享的对象。一旦任意线程命中风控：

1. 当前线程至少等待 `--risk-delay` 秒。
2. 其他线程在发起下一次请求前也会检查冷却时间。
3. 冷却期未结束时，所有线程停止继续请求。

默认风控冷却时间为 60 秒。

## 8. 作者和视频字段整理

### 8.1 作者字段

`compact_author()` 从 `card` 接口中保留：

- `uid`
- `name`
- `face`
- `fans`
- `like_num`
- `level`
- `sign`
- `description`
- `official`
- `archive_count`
- `article_count`
- `space_url`

作者认证字段兼容 `Official`、`official` 和 `official_verify` 三种可能的键名。

### 8.2 视频字段

`compact_video()` 从 `view` 接口中保留：

- `bvid`
- `title`
- `pic`
- `pubdate`
- `desc`
- `duration`
- `owner`
- `stat`
- `argue_info`
- `is_cooperation`
- `staff`
- `status`
- `error`
- `fetched_at`
- `creator_relation`

`creator_relation` 用来描述当前输入作者与视频的关系：

- `owner`：输入 UID 等于视频主投稿人 UID。
- `staff`：输入 UID 出现在联合投稿 `staff` 中。
- `unverified`：接口返回结果中无法确认该作者与视频的关系。

跨作者重复 BV 号不会删除。同一视频可能分别出现在主投稿人与联合投稿人的作者文件中。

## 9. 作者状态判定

每位作者最终有三种状态：

| 状态 | 含义 |
| --- | --- |
| `success` | 作者资料成功，并且全部视频成功 |
| `partial_failed` | 作者资料或部分视频成功，但仍有失败项 |
| `failed` | 作者资料失败，并且没有任何视频成功 |

失败信息会保存为结构化对象：

```json
{
  "kind": "timeout",
  "message": "请求超时",
  "attempts": 3,
  "http_status": null,
  "api_code": null,
  "time": "2026-09-08T12:00:00Z"
}
```

常见 `kind` 包括：

- `http`
- `api`
- `timeout`
- `network`
- `response`
- `parse`
- `worker`

## 10. 输出目录和作者文件

输出目录结构如下：

```text
video_batches_result/video_batches_0/
├── manifest.json
└── creators/
    ├── 8542312.json
    ├── 836885.json
    ├── 523373746.json
    └── ...
```

每个作者文件包含作者资料、该作者的全部视频结果和统计：

```json
{
  "schema_version": 1,
  "uid": "8542312",
  "status": "success",
  "author": {
    "uid": "8542312",
    "name": "作者名称"
  },
  "author_error": null,
  "videos": [
    {
      "bvid": "BV1QGgy6uEMy",
      "status": "success",
      "creator_relation": "owner",
      "title": "视频标题"
    }
  ],
  "summary": {
    "video_total": 1,
    "video_success": 1,
    "video_failed": 0
  },
  "started_at": "2026-09-08T11:59:10Z",
  "finished_at": "2026-09-08T11:59:10Z"
}
```

## 11. 原子写盘机制

`atomic_write()` 不会直接覆盖正式文件，而是：

1. 在目标目录创建临时文件。
2. 将完整 JSON 写入临时文件。
3. `flush` 并调用 `fsync`，尽量确保内容已写入磁盘。
4. 使用 `replace` 原子替换正式文件。

如果写入过程中出现异常，临时文件会被删除，原来的正式结果仍然保留。这可以避免断电或 Ctrl+C 导致作者 JSON 只写了一半。

## 12. 断点续跑机制

默认启用断点续跑，不需要额外参数。

启动时，脚本逐个检查 `creators/<uid>.json`：

- 作者状态为 `success`。
- 作者资料存在。
- 输出中的 BV 号列表与当前输入完全一致且顺序相同。
- 所有视频状态均为 `success`。

同时满足这些条件时，该作者被整体跳过。

对于 `partial_failed` 或 `failed` 作者：

- 已成功的作者资料会被复用。
- 已成功的视频会被复用。
- 只重新请求缺失或失败的部分。
- 作者处理结束后，使用新结果原子替换旧文件。

需要忽略所有断点、强制刷新时使用：

```bash
uv run python bili_video_batch_extractor.py \
  --input video_batches/video_batches_0.json \
  --refresh
```

断点粒度是“一位作者”。如果程序在某位作者处理中途退出，本轮尚未完成的内容不会写入；已有的旧作者文件不会损坏。下次运行会从该作者上一次完整保存的状态继续。

## 13. manifest.json

每完成一位作者，主线程都会更新一次 `manifest.json`。其中包含：

- 输入文件绝对路径。
- 输入作者数、视频数和重复统计。
- 本次选择的作者数和视频数。
- 实际运行参数。
- 已完成、剩余、成功、部分失败和失败作者数。
- 当前任务状态。

`run.state` 可能为：

| 状态 | 含义 |
| --- | --- |
| `running` | 任务仍在运行，或上次运行未正常结束 |
| `success` | 当前输入中的所有作者均成功 |
| `completed_with_errors` | 全部作者处理结束，但存在失败项 |
| `interrupted` | 用户通过 Ctrl+C 中断任务 |

如果指定的输出目录中已有 `manifest.json`，脚本会检查其输入路径。若该目录属于另一个输入文件，任务会拒绝启动，避免不同批次意外混写。

## 14. Ctrl+C 中断行为

收到 Ctrl+C 后：

1. 设置全局停止事件。
2. 取消尚未开始的作者任务。
3. 正在等待请求间隔、重试或风控冷却的线程会尽快退出。
4. 等待已经进入网络请求的线程返回或超时。
5. 将 manifest 状态更新为 `interrupted`。

之前已完成并落盘的作者不会丢失。之后使用相同命令即可继续。

## 15. 命令行参数

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `--input`, `-i` | 必填 | 输入批次 JSON |
| `--output-dir`, `-o` | `video_batches_result/<输入名>` | 输出目录 |
| `--workers` | `8` | 同时处理的作者数 |
| `--retries` | `3` | 单次 API 请求最大尝试次数 |
| `--retry-delay` | `2.0` | 普通错误首次退避秒数 |
| `--risk-delay` | `60.0` | 命中风控后的全局冷却秒数 |
| `--timeout` | `15.0` | 单次 HTTP 请求超时秒数 |
| `--request-delay-min` | `0.2` | 同一作者相邻请求最短间隔 |
| `--request-delay-max` | `0.6` | 同一作者相邻请求最长间隔 |
| `--refresh` | 关闭 | 忽略已有成功结果并重新请求 |
| `--max-creators` | 不限制 | 只处理前 N 位作者 |
| `--max-videos` | 不限制 | 每位作者只处理前 N 个视频 |

查看脚本自身帮助：

```bash
uv run python bili_video_batch_extractor.py --help
```

## 16. 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 全部作者成功 |
| `1` | 输入、参数、已有结果或文件系统错误，任务未正常启动 |
| `2` | 任务完成，但存在 `partial_failed` 或 `failed` 作者 |
| `130` | 用户通过 Ctrl+C 中断 |

## 17. 参数调整建议

- 首次验证使用 `--workers 2 --max-creators 2 --max-videos 3`。
- 正常运行可从默认的 8 个线程开始观察。
- 如果频繁出现 `-352`、`-412`、`-509`、HTTP 403 或 429，应降低 `--workers`，增大请求间隔和 `--risk-delay`。
- 如果网络不稳定但没有风控，可适当增加 `--retries` 和 `--timeout`。
- 不建议仅为追求速度无限提高线程数；整体速度还受接口响应时间、请求间隔和风控限制。

一个相对保守的运行示例：

```bash
uv run python bili_video_batch_extractor.py \
  --input video_batches/video_batches_0.json \
  --workers 4 \
  --retries 3 \
  --request-delay-min 0.5 \
  --request-delay-max 1.0 \
  --risk-delay 120
```
