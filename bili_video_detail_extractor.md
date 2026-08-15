# Bilibili 作者分组作品详情提取说明

`bili_video_detail_extractor.py` 用于读取“作者 UID + 作品列表”JSON，按作者分类保存作者资料、作品详情、互动数据，以及视频播放/下载候选地址。脚本只保存地址，不下载视频文件。

## 输入格式

推荐格式如下。JSON 的键和值必须使用双引号：

```json
[
  {
    "uid": "434377496",
    "videos": [
      "BV1xxxxxxxxx",
      "https://www.bilibili.com/video/BV1yyyyyyyyy",
      {"bvid": "BV1zzzzzzzzz"},
      {"aid": 123456789}
    ]
  },
  {
    "uid": "23972272",
    "videos": []
  }
]
```

也支持 `bili_creator_demo.py` 的完整输出：

```json
{
  "creators": [
    {
      "uid": "434377496",
      "videos": [
        {"aid": 123, "bvid": "BV...", "url": "https://www.bilibili.com/video/BV..."}
      ]
    }
  ]
}
```

每个 `videos` 元素可为 URL、BV 号、av 号、纯 aid，或包含 `url`、`bvid`、`aid` 的对象。脚本会在每个作者内按 BV/aid 去重。

## 运行方式

首次运行建议保留浏览器界面。若缓存中没有有效登录态，脚本会打开 B 站首页并等待登录：

```bash
uv run python bili_video_detail_extractor.py \
  --input bili_creator_videos.json \
  --output bili_video_details.json
```

也可以直接传入 Cookie：

```bash
uv run python bili_video_detail_extractor.py \
  -i input.json \
  -o output.json \
  --cookie "SESSDATA=...; bili_jct=..."
```

已有有效的 `chrome_data` 登录态时可无界面运行：

```bash
uv run python bili_video_detail_extractor.py -i input.json -o output.json --headless
```

常用参数：

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `--qn` | `80` | 请求画质，80 通常表示 1080P |
| `--retries` | `3` | 每个 API 的最大尝试次数 |
| `--retry-delay` | `2` | 普通错误的首次退避秒数，后续指数增加 |
| `--risk-control-delay` | `300` | B 站返回 `-352` 后的冷却秒数 |
| `--video-sleep-min/max` | `0.8/2.0` | 相邻视频请求之间的随机间隔 |
| `--no-resume` | 关闭 | 忽略旧输出并从头抓取 |
| `--refresh-success` | 关闭 | 重抓成功视频，用于刷新有时效的播放地址 |

## 抓取与保存流程

```text
读取作者分组 JSON
  -> 启动持久浏览器并取得 Cookie、UA、WBI key
  -> 每个 UID 调用一次 /x/web-interface/card
  -> 每个视频调用 /x/web-interface/view
  -> 分别调用 playurl 获取 durl 与 DASH
  -> 每完成一个步骤便原子写入输出 JSON
```

作者资料不会随每个视频重复请求。输出中的 `author` 包括：

- `name`：昵称
- `face`：头像
- `fans`：粉丝数
- `like_num`：累计获赞数
- `level`：账号等级
- `sign` / `description`：签名、简介
- `official`：认证信息
- `archive_count` / `article_count`：投稿及专栏数量

作品数据包括：

- 基础信息：标题、简介、发布时间、封面、时长、`aid`、`bvid`、`cid`
- 联合投稿：`is_union_video`、`cooperation_authors`
- 互动数据：播放、点赞、投币、收藏、分享、弹幕、评论数
- 分 P 信息：`pages`
- 播放地址：`play_urls.durl`、`play_urls.dash.video`、`play_urls.dash.audio`

传统 `durl` 与 DASH 通常不会同时出现在一次播放接口响应中，因此脚本分别请求：

- `fnval=1`：提取 `durl` 直链
- `fnval=4048`：提取 DASH 视频流和音频流

DASH 视频和音频是分离的，真正下载后还需要使用 FFmpeg 等工具合并。本脚本不执行下载或合并。

## 断点续跑与错误处理

输出文件本身就是断点文件，不额外创建状态数据库：

- 作者资料请求成功后立即写盘，此后续跑不再请求该作者资料。
- 每个视频开始前写入 `running`，完成后写入 `success` 或 `failed`。
- 使用相同输入和输出命令重启时，自动跳过 `success`，重新抓取 `failed`、`running` 和 `pending`。
- 单个视频失败不会阻断同作者或后续作者。
- 普通网络/API 错误采用指数退避；`-352` 风控采用长冷却，并刷新浏览器 Cookie 与 WBI 签名。
- 写文件使用“同目录临时文件 + 原子替换”，降低强制退出时损坏 JSON 的概率。

播放地址通常带有时效。若详情已完成但需要刷新地址，运行原命令并增加：

```bash
--refresh-success
```

## 输出结构示例

```json
{
  "creators": [
    {
      "uid": "434377496",
      "author": {
        "uid": "434377496",
        "name": "作者昵称",
        "face": "https://...",
        "fans": 10000,
        "like_num": 200000,
        "level": 6,
        "sign": "简介"
      },
      "status": "success",
      "video_count": 1,
      "success_count": 1,
      "failed_count": 0,
      "videos": [
        {
          "source": "BV1...",
          "status": "success",
          "aid": 123,
          "bvid": "BV1...",
          "cid": 456,
          "title": "作品标题",
          "is_union_video": false,
          "cooperation_authors": [],
          "stats": {
            "view": 100,
            "like": 10,
            "coin": 2,
            "favorite": 3,
            "share": 1,
            "danmaku": 4,
            "reply": 5
          },
          "play_urls": {
            "durl": {"items": []},
            "dash": {"video": [], "audio": []}
          }
        }
      ]
    }
  ]
}
```

## 与原项目的关系

- `bili_creator_demo.py`：根据 UID 枚举作者作品，生成按作者分类的作品列表。
- `bili_video_detail_extractor.py`：消费上述列表，补全作者资料、每个作品详情和播放地址。
- `bili_signin.py`：提供 WBI 参数签名。
- `chrome_data/`：持久保存浏览器登录态，两个脚本可共用。

这样作者作品枚举和作品详情补全保持两个独立阶段，前一阶段的结果也可以直接作为后一阶段输入。
