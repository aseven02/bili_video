"""统计 video_batches_result 中的作者、视频及 AI 使用情况。"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import pathlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
VIDEO_METRICS = ("duration", "view", "like", "coin", "favorite", "share", "danmaku", "reply")
FAN_BANDS = ((1_000, "<1千"), (10_000, "1千-1万"), (100_000, "1万-10万"),
             (1_000_000, "10万-100万"), (10_000_000, "100万-1000万"), (math.inf, "1000万以上"))
VIEW_BANDS = ((1_000, "<1千"), (10_000, "1千-1万"), (100_000, "1万-10万"),
              (1_000_000, "10万-100万"), (10_000_000, "100万-1000万"), (math.inf, "1000万以上"))
DURATION_BANDS = ((60, "<1分钟"), (300, "1-5分钟"), (600, "5-10分钟"),
                  (1_800, "10-30分钟"), (3_600, "30-60分钟"), (math.inf, "60分钟以上"))
VIDEO_COUNT_BANDS = ((10, "<10"), (50, "10-49"), (100, "50-99"),
                     (200, "100-199"), (400, "200-399"), (math.inf, "400及以上"))


@dataclass
class NumericStats:
    """在线计算 count/min/max/sum/mean/std，避免保存全部数值。"""

    count: int = 0
    total: float = 0
    minimum: float | None = None
    maximum: float | None = None
    mean: float = 0
    m2: float = 0

    def add(self, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        number = float(value)
        self.count += 1
        self.total += number
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)
        delta = number - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (number - self.mean)

    def result(self, total_records: int) -> dict[str, int | float | None]:
        return {
            "count": self.count,
            "missing": total_records - self.count,
            "min": clean_number(self.minimum),
            "max": clean_number(self.maximum),
            "sum": clean_number(self.total),
            "mean": round(self.mean, 2) if self.count else None,
            "std_dev": round(math.sqrt(self.m2 / self.count), 2) if self.count else None,
        }


class TopItems:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.sequence = 0
        self.items: list[tuple[float, int, dict[str, Any]]] = []

    def add(self, value: Any, item: dict[str, Any]) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        self.sequence += 1
        entry = (float(value), self.sequence, {**item, "value": value})
        if len(self.items) < self.limit:
            heapq.heappush(self.items, entry)
        elif entry[0] > self.items[0][0]:
            heapq.heapreplace(self.items, entry)

    def result(self) -> list[dict[str, Any]]:
        return [item for _, _, item in sorted(self.items, reverse=True)]


def clean_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else round(value, 2)


def rate(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): counter[key] for key in sorted(counter, key=lambda value: str(value))}


def band(value: Any, bands: tuple[tuple[float, str], ...]) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return next(label for upper, label in bands if value < upper)


def band_result(counter: Counter[str], bands: tuple[tuple[float, str], ...]) -> dict[str, int]:
    return {label: counter[label] for _, label in bands}


def ai_category(message: str) -> str | None:
    lowered = message.lower()
    if "ai" not in lowered and "人工智能" not in message:
        return None
    if "疑似" in message or "可能" in message:
        return "suspected"
    if "作者声明" in message:
        return "author_declared"
    if "含ai生成内容" in lowered:
        return "platform_labeled"
    return "other_ai_notice"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计 Bilibili 作者、视频和 AI 使用情况")
    parser.add_argument("--input-dir", default="video_batches_result", help="结果根目录或单个批次目录")
    parser.add_argument("--output", default="video_batches_descriptive_stats.json", help="统计结果 JSON")
    parser.add_argument("--top-n", type=int, default=20, help="排行榜保留数量")
    parser.add_argument(
        "--since-year",
        type=int,
        help="只统计该年份及之后发布的视频，以及至少有一个符合条件作品的作者",
    )
    args = parser.parse_args()
    if args.top_n < 1:
        parser.error("--top-n 必须大于 0")
    if args.since_year is not None and not 1970 <= args.since_year <= 2100:
        parser.error("--since-year 必须在 1970 到 2100 之间")
    return args


def discover_files(root: pathlib.Path) -> list[pathlib.Path]:
    files = (
        sorted((root / "creators").glob("*.json"))
        if (root / "creators").is_dir()
        else sorted(root.glob("video_batches_*/creators/*.json"))
    )
    if not files:
        raise FileNotFoundError(f"没有找到作者结果 JSON: {root}")
    return files


def error_key(error: Any) -> str | None:
    if not isinstance(error, dict):
        return None
    if error.get("http_status") is not None:
        return f"http:{error['http_status']}"
    if error.get("api_code") is not None:
        return f"api:{error['api_code']}"
    return str(error.get("kind") or "unknown")


def video_year(video: dict[str, Any]) -> int | None:
    value = video.get("pubdate")
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, LOCAL_TZ).year
    return None


def metric_group() -> dict[str, NumericStats]:
    return {name: NumericStats() for name in VIDEO_METRICS}


def add_video_metrics(metrics: dict[str, NumericStats], video: dict[str, Any]) -> dict[str, Any]:
    stats = video.get("stat") if isinstance(video.get("stat"), dict) else {}
    metrics["duration"].add(video.get("duration"))
    for name in VIDEO_METRICS[1:]:
        metrics[name].add(stats.get(name))
    return stats


def analyze(files: list[pathlib.Path], top_n: int, since_year: int | None) -> dict[str, Any]:
    author_statuses: Counter[str] = Counter()
    author_levels: Counter[Any] = Counter()
    author_errors: Counter[str] = Counter()
    video_statuses: Counter[str] = Counter()
    video_relations: Counter[str] = Counter()
    video_errors: Counter[str] = Counter()
    publish_years: Counter[int] = Counter()
    argue_messages: Counter[str] = Counter()
    ai_categories: Counter[str] = Counter()
    ai_years: Counter[int] = Counter()
    fan_bands: Counter[str] = Counter()
    author_video_bands: Counter[str] = Counter()
    view_bands: Counter[str] = Counter()
    duration_bands: Counter[str] = Counter()
    batches: dict[str, Counter[str]] = {}
    seen_uids: set[str] = set()
    seen_bvids: set[str] = set()
    successful_bvids: set[str] = set()
    cooperation_bvids: set[str] = set()
    cooperation_author_uids: set[str] = set()
    ai_author_uids: set[str] = set()
    invalid_files: list[dict[str, str]] = []

    author_metrics = {
        name: NumericStats()
        for name in ("fans", "like_num", "archive_count", "article_count", "selected_video_records")
    }
    video_metrics = metric_group()
    ai_metrics = metric_group()
    non_ai_metrics = metric_group()
    top_authors_fans, top_authors_likes = TopItems(top_n), TopItems(top_n)
    top_videos_views, top_videos_likes, top_ai_views = TopItems(top_n), TopItems(top_n), TopItems(top_n)
    top_cooperation_views, top_cooperation_likes = TopItems(top_n), TopItems(top_n)
    profile_count = certified_count = cooperation_count = argue_count = ai_count = 0
    earliest_pubdate: int | None = None
    latest_pubdate: int | None = None
    author_records = video_records = 0
    excluded_authors = excluded_before_year = excluded_missing_date = 0

    for path in files:
        batch_name = path.parent.parent.name
        batch_counts = batches.setdefault(batch_name, Counter())
        try:
            with path.open(encoding="utf-8") as file:
                result = json.load(file)
            if not isinstance(result, dict) or not isinstance(result.get("videos"), list):
                raise ValueError("根节点不是有效作者结果")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            invalid_files.append({"path": str(path), "error": str(error)})
            batch_counts["invalid_files"] += 1
            continue

        uid = str(result.get("uid") or "")
        if uid in seen_uids:
            batch_counts["duplicate_author_records"] += 1
            continue
        seen_uids.add(uid)

        selected_videos: list[dict[str, Any]] = []
        for video in result["videos"]:
            if not isinstance(video, dict):
                continue
            if since_year is None:
                selected_videos.append(video)
                continue
            year = video_year(video)
            if year is None:
                excluded_missing_date += 1
            elif year >= since_year:
                selected_videos.append(video)
            else:
                excluded_before_year += 1
        if since_year is not None and not selected_videos:
            excluded_authors += 1
            continue

        author_records += 1
        status = str(result.get("status") or "unknown")
        author_statuses[status] += 1
        batch_counts["authors"] += 1
        batch_counts[f"authors_{status}"] += 1

        author = result.get("author")
        author_name = author.get("name") if isinstance(author, dict) else None
        if isinstance(author, dict):
            profile_count += 1
            for name in ("fans", "like_num", "archive_count", "article_count"):
                author_metrics[name].add(author.get(name))
            author_levels[author.get("level")] += 1
            fan_label = band(author.get("fans"), FAN_BANDS)
            if fan_label:
                fan_bands[fan_label] += 1
            official = author.get("official") if isinstance(author.get("official"), dict) else {}
            if official.get("title") or official.get("type") not in (None, -1):
                certified_count += 1
            identity = {
                "uid": uid,
                "name": author_name,
                "face": author.get("face"),
                "space_url": author.get("space_url") or f"https://space.bilibili.com/{uid}",
            }
            top_authors_fans.add(author.get("fans"), identity)
            top_authors_likes.add(author.get("like_num"), identity)
        else:
            key = error_key(result.get("author_error"))
            if key:
                author_errors[key] += 1

        author_metrics["selected_video_records"].add(len(selected_videos))
        count_label = band(len(selected_videos), VIDEO_COUNT_BANDS)
        if count_label:
            author_video_bands[count_label] += 1

        for video in selected_videos:
            video_records += 1
            video_status = str(video.get("status") or "unknown")
            video_statuses[video_status] += 1
            batch_counts["videos"] += 1
            batch_counts[f"videos_{video_status}"] += 1
            bvid = str(video.get("bvid") or "")
            if bvid:
                seen_bvids.add(bvid)
            if video_status != "success":
                key = error_key(video.get("error"))
                if key:
                    video_errors[key] += 1
                continue

            if bvid:
                successful_bvids.add(bvid)

            relation = str(video.get("creator_relation") or "unknown")
            video_relations[relation] += 1
            is_cooperation = video.get("is_cooperation") in (True, 1)
            if is_cooperation:
                cooperation_count += 1
                cooperation_author_uids.add(uid)
            stats = add_video_metrics(video_metrics, video)
            view_label = band(stats.get("view"), VIEW_BANDS)
            duration_label = band(video.get("duration"), DURATION_BANDS)
            if view_label:
                view_bands[view_label] += 1
            if duration_label:
                duration_bands[duration_label] += 1

            pubdate = video.get("pubdate")
            year = video_year(video)
            if year is not None and isinstance(pubdate, int):
                publish_years[year] += 1
                earliest_pubdate = pubdate if earliest_pubdate is None else min(earliest_pubdate, pubdate)
                latest_pubdate = pubdate if latest_pubdate is None else max(latest_pubdate, pubdate)

            message = str((video.get("argue_info") or {}).get("argue_msg") or "").strip()
            category = ai_category(message)
            if message:
                argue_count += 1
                argue_messages[message] += 1
            if category:
                ai_count += 1
                ai_author_uids.add(uid)
                ai_categories[category] += 1
                if year is not None:
                    ai_years[year] += 1
                add_video_metrics(ai_metrics, video)
            else:
                add_video_metrics(non_ai_metrics, video)

            identity = {
                "bvid": bvid,
                "title": video.get("title"),
                "pic": video.get("pic"),
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else None,
                "creator_uid": uid,
                "creator_name": author_name,
            }
            top_videos_views.add(stats.get("view"), identity)
            top_videos_likes.add(stats.get("like"), identity)
            if is_cooperation and bvid and bvid not in cooperation_bvids:
                cooperation_bvids.add(bvid)
                owner = video.get("owner") if isinstance(video.get("owner"), dict) else {}
                cooperation_identity = {
                    **identity,
                    "creator_uid": str(owner.get("mid") or uid),
                    "creator_name": owner.get("name") or author_name,
                }
                top_cooperation_views.add(stats.get("view"), cooperation_identity)
                top_cooperation_likes.add(stats.get("like"), cooperation_identity)
            if category:
                top_ai_views.add(stats.get("view"), {**identity, "ai_notice": message})

    successful_videos = video_statuses["success"]
    filter_info = {
        "since_year": since_year,
        "inclusive": True,
        "timezone": str(LOCAL_TZ),
        "excluded_authors_without_qualifying_videos": excluded_authors,
        "excluded_videos_before_year": excluded_before_year,
        "excluded_videos_without_pubdate": excluded_missing_date,
    }
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "files_discovered": len(files),
            "files_analyzed": len(files) - len(invalid_files),
            "invalid_file_count": len(invalid_files),
            "invalid_files": invalid_files,
            "filter": filter_info,
        },
        "authors": {
            "records": author_records,
            "profiles_available": profile_count,
            "profile_availability_rate": rate(profile_count, author_records),
            "status_counts": sorted_counter(author_statuses),
            "certified_count": certified_count,
            "certified_rate": rate(certified_count, profile_count),
            "level_counts": sorted_counter(author_levels),
            "error_counts": sorted_counter(author_errors),
            "distributions": {
                "fans": band_result(fan_bands, FAN_BANDS),
                "selected_video_records": band_result(author_video_bands, VIDEO_COUNT_BANDS),
            },
            "metrics": {name: metric.result(profile_count if name != "selected_video_records" else author_records)
                        for name, metric in author_metrics.items()},
            "top_by_fans": top_authors_fans.result(),
            "top_by_like_num": top_authors_likes.result(),
        },
        "videos": {
            "records": video_records,
            "unique_bvids": len(seen_bvids),
            "duplicate_associations": video_records - len(seen_bvids),
            "status_counts": sorted_counter(video_statuses),
            "success_rate": rate(successful_videos, video_records),
            "creator_relation_counts": sorted_counter(video_relations),
            "cooperation_count": cooperation_count,
            "cooperation_rate": rate(cooperation_count, successful_videos),
            "publish_time": {
                "earliest": timestamp_text(earliest_pubdate),
                "latest": timestamp_text(latest_pubdate),
                "year_counts": sorted_counter(publish_years),
            },
            "distributions": {
                "views": band_result(view_bands, VIEW_BANDS),
                "duration": band_result(duration_bands, DURATION_BANDS),
            },
            "error_counts": sorted_counter(video_errors),
            "metrics": {name: metric.result(successful_videos) for name, metric in video_metrics.items()},
            "top_by_views": top_videos_views.result(),
            "top_by_likes": top_videos_likes.result(),
        },
        "cooperation": {
            "authors": len(cooperation_author_uids),
            "author_rate": rate(len(cooperation_author_uids), author_records),
            "video_associations": cooperation_count,
            "association_rate": rate(cooperation_count, successful_videos),
            "successful_unique_videos": len(successful_bvids),
            "unique_videos": len(cooperation_bvids),
            "unique_video_rate": rate(len(cooperation_bvids), len(successful_bvids)),
            "top_by_views": top_cooperation_views.result(),
            "top_by_likes": top_cooperation_likes.result(),
        },
        "ai_usage": {
            "videos": ai_count,
            "video_rate": rate(ai_count, successful_videos),
            "authors": len(ai_author_uids),
            "author_rate": rate(len(ai_author_uids), author_records),
            "argue_info_videos": argue_count,
            "category_counts": sorted_counter(ai_categories),
            "notice_counts": dict(argue_messages.most_common()),
            "year_counts": sorted_counter(ai_years),
            "metrics": {
                "ai": {name: metric.result(ai_count) for name, metric in ai_metrics.items()},
                "non_ai": {name: metric.result(successful_videos - ai_count)
                           for name, metric in non_ai_metrics.items()},
            },
            "top_by_views": top_ai_views.result(),
        },
        "batches": {name: sorted_counter(counts) for name, counts in sorted(batches.items())},
    }


def timestamp_text(value: int | None) -> dict[str, int | str] | None:
    if value is None:
        return None
    return {"timestamp": value, "datetime": datetime.fromtimestamp(value, LOCAL_TZ).isoformat()}


def main() -> int:
    args = parse_args()
    input_dir = pathlib.Path(args.input_dir).expanduser().resolve()
    output = pathlib.Path(args.output).expanduser().resolve()
    try:
        result = analyze(discover_files(input_dir), args.top_n, args.since_year)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"统计失败: {error}")
        return 1

    authors, videos = result["authors"], result["videos"]
    cooperation, ai = result["cooperation"], result["ai_usage"]
    success_rate = videos["success_rate"] or 0
    print(f"作者: {authors['records']:,}，视频: {videos['records']:,}，成功率: {success_rate:.2%}")
    print(f"参与联合投稿作者: {cooperation['authors']:,}，占比: {cooperation['author_rate']:.2%}")
    print(f"AI 相关视频: {ai['videos']:,}，涉及作者: {ai['authors']:,}")
    print(f"统计结果: {output}")
    return 2 if result["source"]["invalid_file_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
