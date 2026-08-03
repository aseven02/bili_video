# bili-video

Small Bilibili creator video crawler demo.

## Batch Creator Crawl

Single UID:

```bash
uv run python bili_creator_demo.py --creator 434377496 --max-pages 2
```

Multiple UIDs:

```bash
uv run python bili_creator_demo.py --creators 434377496,23972272 --max-pages 2
```

JSON UID list:

```bash
uv run python bili_creator_demo.py --uid-json uids.json --output output/bili_creator_videos.json
```

`uids.json` supports either format:

```json
["434377496", "23972272"]
```

```json
{"uids": ["434377496", "23972272"]}
```

The crawler uses a persistent Playwright profile at `browser_data/bili_creator_demo_user_data`.
On the first run, log in through the opened browser window. Later runs reuse that login state.

Output is a regular JSON file. Each creator item has this main shape:

```json
{
  "uid": "434377496",
  "videos": [
    {
      "aid": 123,
      "bvid": "BV...",
      "title": "...",
      "created": 1784820893,
      "length": "03:35",
      "play": 182035,
      "comment": 2985,
      "description": "...",
      "is_union_video": false,
      "url": "https://www.bilibili.com/video/BV..."
    }
  ]
}
```

See [hep.md](hep.md) for the full usage notes and implementation details.
