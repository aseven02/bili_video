from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
from collections import Counter
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_INPUT_DIR = pathlib.Path("creator_batches")
DEFAULT_OUTPUT = pathlib.Path("uid_info.json")
DEFAULT_STATS_OUTPUT = pathlib.Path("creator_batch_stats.json")
VIDEO_FIELDS = (
    "aid",
    "bvid",
    "title",
    "created",
    "length",
    "play",
    "comment",
    "description",
    "is_union_video",
    "url",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge Bilibili creator batch files and print descriptive stats")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="Directory containing batch*.json files")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Merged uid info JSON output path")
    parser.add_argument("--stats-output", default=str(DEFAULT_STATS_OUTPUT), help="Stats JSON output path")
    parser.add_argument("--since", default="2025-01-01", help="Count videos created on or after this date, YYYY-MM-DD")
    return parser.parse_args()


def natural_key(path: pathlib.Path) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def parse_since(value: str) -> int:
    # The CLI accepts a calendar date. Treat it as local midnight in China time,
    # then compare against Bilibili's Unix-second `created` field.
    date = datetime.strptime(value, "%Y-%m-%d").date()
    return int(datetime.combine(date, time.min, tzinfo=LOCAL_TZ).timestamp())


def load_batch(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    creators = data.get("creators", [])
    if not isinstance(creators, list):
        raise ValueError(f"{path} has unsupported creators type: {type(creators).__name__}")
    return data


def video_key(uid: str, video: dict[str, Any]) -> str:
    # Prefer stable platform ids for deduplication. The fallback keeps records
    # without aid/bvid from collapsing across different creators.
    bvid = video.get("bvid")
    if bvid:
        return f"bvid:{bvid}"
    aid = video.get("aid")
    if aid:
        return f"aid:{aid}"
    return f"fallback:{uid}:{video.get('created')}:{video.get('title')}"


def compact_video(video: dict[str, Any]) -> dict[str, Any]:
    # Keep only fields useful for downstream analysis and add a readable local
    # timestamp while preserving the original Unix timestamp for computation.
    item = {key: video.get(key) for key in VIDEO_FIELDS if key in video}
    created = item.get("created")
    if isinstance(created, int | float):
        item["created_at"] = datetime.fromtimestamp(created, tz=LOCAL_TZ).isoformat()
    return item


def is_video_since(video: dict[str, Any], since_ts: int) -> bool:
    # "Since YYYY-MM-DD" is inclusive: videos created at exactly local midnight
    # on that date are counted.
    created = video.get("created")
    return isinstance(created, int | float) and created >= since_ts


def merge_creator(target: dict[str, Any], source: dict[str, Any], batch_name: str) -> tuple[int, int]:
    uid = str(source.get("uid", "")).strip()
    existing_video_keys = {video_key(uid, video) for video in target["videos"]}
    added = 0
    duplicated = 0

    for raw_video in source.get("videos") or []:
        if not isinstance(raw_video, dict):
            continue
        key = video_key(uid, raw_video)
        # A creator can appear in multiple batches; avoid writing the same video
        # twice while still recording every crawl attempt below.
        if key in existing_video_keys:
            duplicated += 1
            continue
        existing_video_keys.add(key)
        target["videos"].append(compact_video(raw_video))
        added += 1

    target["batches"].append(batch_name)
    target["crawl_records"].append(
        {
            "batch": batch_name,
            "status": source.get("status"),
            "total": source.get("total"),
            "video_count": len(source.get("videos") or []),
            "pages_crawled": source.get("pages_crawled") or [],
            "started_at": source.get("started_at"),
            "finished_at": source.get("finished_at"),
            "last_page_finished_at": source.get("last_page_finished_at"),
            "error": source.get("error"),
        }
    )
    target["statuses"] = sorted({*target["statuses"], str(source.get("status") or "unknown")})
    target["declared_total"] = max_int(target.get("declared_total"), source.get("total"))
    return added, duplicated


def max_int(left: Any, right: Any) -> int | None:
    values = [value for value in (left, right) if isinstance(value, int)]
    return max(values) if values else None


def make_creator(uid: str) -> dict[str, Any]:
    return {
        "uid": uid,
        "statuses": [],
        "declared_total": None,
        "video_count": 0,
        "videos_since_count": 0,
        "batches": [],
        "crawl_records": [],
        "videos": [],
    }


def summarize_video_dates(videos: list[dict[str, Any]]) -> dict[str, str | int | None]:
    timestamps = [video.get("created") for video in videos if isinstance(video.get("created"), int)]
    if not timestamps:
        return {"earliest_created": None, "latest_created": None}
    earliest = min(timestamps)
    latest = max(timestamps)
    return {
        "earliest_created": earliest,
        "earliest_created_at": datetime.fromtimestamp(earliest, tz=LOCAL_TZ).isoformat(),
        "latest_created": latest,
        "latest_created_at": datetime.fromtimestamp(latest, tz=LOCAL_TZ).isoformat(),
    }


def build_stats(
    merged: dict[str, dict[str, Any]],
    batch_stats: list[dict[str, Any]],
    since_ts: int,
    since: str,
    input_video_records: int,
    duplicate_author_records: int,
    duplicate_video_records: int,
) -> dict[str, Any]:
    creators = list(merged.values())
    videos = [video for creator in creators for video in creator["videos"]]
    videos_per_author = [creator["video_count"] for creator in creators]
    status_counter = Counter(status for creator in creators for status in creator["statuses"])
    videos_since = [video for video in videos if is_video_since(video, since_ts)]
    authors_since = sum(1 for creator in creators if creator["videos_since_count"] > 0)
    union_videos_since = sum(1 for video in videos_since if video.get("is_union_video") is True)
    plays = [video.get("play") for video in videos if isinstance(video.get("play"), int)]
    comments = [video.get("comment") for video in videos if isinstance(video.get("comment"), int)]
    union_videos = sum(1 for video in videos if video.get("is_union_video") is True)

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input": {
            "batch_count": len(batch_stats),
            "batch_files": [item["file"] for item in batch_stats],
            "input_creator_records": sum(item["creator_records"] for item in batch_stats),
            "input_video_records": input_video_records,
            "duplicate_author_records": duplicate_author_records,
            "duplicate_video_records": duplicate_video_records,
        },
        "overall": {
            "author_count": len(creators),
            "status_counts": dict(sorted(status_counter.items())),
            "total_video_count": len(videos),
            "authors_with_videos": sum(1 for count in videos_per_author if count > 0),
            "authors_without_videos": sum(1 for count in videos_per_author if count == 0),
            "videos_per_author": {
                "min": min(videos_per_author) if videos_per_author else 0,
                "max": max(videos_per_author) if videos_per_author else 0,
                "mean": round(statistics.fmean(videos_per_author), 2) if videos_per_author else 0,
                "median": statistics.median(videos_per_author) if videos_per_author else 0,
            },
            "total_play": sum(plays),
            "total_comment": sum(comments),
            "union_video_count": union_videos,
            **summarize_video_dates(videos),
        },
        "since": {
            "date": since,
            "timestamp": since_ts,
            "datetime": datetime.fromtimestamp(since_ts, tz=LOCAL_TZ).isoformat(),
            "total_video_count": len(videos_since),
            "author_count_with_videos": authors_since,
            "total_play": sum(video.get("play") for video in videos_since if isinstance(video.get("play"), int)),
            "total_comment": sum(
                video.get("comment") for video in videos_since if isinstance(video.get("comment"), int)
            ),
            "union_video_count": union_videos_since,
        },
        "batches": batch_stats,
    }


def merge_batches(input_dir: pathlib.Path, since_ts: int, since: str) -> tuple[dict[str, Any], dict[str, Any]]:
    files = sorted(input_dir.glob("*.json"), key=natural_key)
    if not files:
        raise FileNotFoundError(f"no json files found in {input_dir}")

    merged: dict[str, dict[str, Any]] = {}
    batch_stats: list[dict[str, Any]] = []
    input_video_records = 0
    duplicate_author_records = 0
    duplicate_video_records = 0

    for path in files:
        data = load_batch(path)
        creators = data["creators"]
        batch_video_records = 0
        batch_success = 0
        batch_failed = 0

        for creator in creators:
            if not isinstance(creator, dict):
                continue
            uid = str(creator.get("uid", "")).strip()
            if not uid:
                continue
            videos = creator.get("videos") or []
            # Batch-level counters describe raw input records before cross-batch
            # video deduplication.
            if len(videos) == 0:
                print(f"Warning: {path.name} has no videos for uid {uid}")
            batch_video_records += len(videos)
            input_video_records += len(videos)
            if creator.get("status") == "success":
                batch_success += 1
            else:
                batch_failed += 1
                print(f"Warning: {path.name} has non-success status for uid {uid}: {creator.get('status')}")

            if uid in merged:
                duplicate_author_records += 1
            else:
                merged[uid] = make_creator(uid)
            _, duplicated = merge_creator(merged[uid], creator, path.name)
            duplicate_video_records += duplicated

        batch_stats.append(
            {
                "file": path.name,
                "generated_at": data.get("generated_at"),
                "updated_at": data.get("updated_at"),
                "creator_records": len(creators),
                "success_records": batch_success,
                "non_success_records": batch_failed,
                "video_records": batch_video_records,
            }
        )

    for creator in merged.values():
        # Final creator-level counters are computed after deduplication and
        # sorting so they match the persisted `videos` list exactly.
        creator["videos"].sort(key=lambda item: item.get("created") or 0, reverse=True)
        creator["video_count"] = len(creator["videos"])
        creator["videos_since_count"] = sum(1 for video in creator["videos"] if is_video_since(video, since_ts))

    stats = build_stats(
        merged,
        batch_stats,
        since_ts,
        since,
        input_video_records,
        duplicate_author_records,
        duplicate_video_records,
    )
    output = {
        "generated_at": stats["generated_at"],
        "source_dir": str(input_dir),
        "since": stats["since"],
        "stats_summary": stats["overall"],
        "creators": dict(sorted(merged.items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0])),
    }
    return output, stats


def write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main() -> None:
    args = parse_args()
    input_dir = pathlib.Path(args.input_dir)
    since_ts = parse_since(args.since)
    merged, stats = merge_batches(input_dir, since_ts, args.since)
    write_json(pathlib.Path(args.output), merged)
    write_json(pathlib.Path(args.stats_output), stats)

    overall = stats["overall"]
    since = stats["since"]
    print(f"作者数: {overall['author_count']}")
    print(f"总作品数量: {overall['total_video_count']}")
    print(f"合作作品数量: {overall['union_video_count']}")
    print(f"{since['date']} 之后作品数量: {since['total_video_count']}")
    print(f"{since['date']} 之后合作作品数量: {since['union_video_count']}")
    print(f"合并输出: {args.output}")
    print(f"统计输出: {args.stats_output}")


if __name__ == "__main__":
    main()
