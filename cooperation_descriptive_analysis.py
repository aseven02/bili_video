"""分析 video_batches_result 中的联合投稿，并生成自包含 HTML 报告。

核心时间序列口径：一部成功作品被标记为联合投稿后，取 owner 与 staff
中的 UID，按作品内去重，再与本次输入文件中的已知作者 UID 集合取交集。
月度、周度作者数均为相应期间内至少参与一部联合作品的唯一已知作者数。
"""

from __future__ import annotations

import argparse
import html
import itertools
import json
import math
import pathlib
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_TZ = "Asia/Shanghai"
METRICS = ("view", "like", "coin", "favorite", "share", "danmaku", "reply")
METRIC_LABELS = {
    "duration": "时长（秒）",
    "view": "播放",
    "like": "点赞",
    "coin": "投币",
    "favorite": "收藏",
    "share": "分享",
    "danmaku": "弹幕",
    "reply": "评论",
}


@dataclass
class Distribution:
    values: array = field(default_factory=lambda: array("d"))
    total: float = 0.0

    def add(self, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        number = float(value)
        if not math.isfinite(number):
            return
        self.values.append(number)
        self.total += number

    def summary(self) -> dict[str, int | float | None]:
        count = len(self.values)
        if not count:
            return {key: None for key in ("count", "min", "p25", "median", "p75", "p90", "p99", "max", "mean")}
        ordered = sorted(self.values)

        def percentile(level: float) -> float:
            index = (count - 1) * level
            lower = math.floor(index)
            upper = math.ceil(index)
            if lower == upper:
                return ordered[lower]
            return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)

        return {
            "count": count,
            "min": clean_number(ordered[0]),
            "p25": clean_number(percentile(0.25)),
            "median": clean_number(percentile(0.50)),
            "p75": clean_number(percentile(0.75)),
            "p90": clean_number(percentile(0.90)),
            "p99": clean_number(percentile(0.99)),
            "max": clean_number(ordered[-1]),
            "mean": round(self.total / count, 2),
        }


def clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else round(value, 2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 Bilibili 联合投稿描述性统计 HTML 报告")
    parser.add_argument("--input-dir", default="video_batches_result", help="抓取结果根目录")
    parser.add_argument(
        "--output",
        default="reports/cooperation_analysis/report.html",
        help="HTML 输出路径",
    )
    parser.add_argument("--timezone", default=DEFAULT_TZ, help="发布日期使用的 IANA 时区")
    parser.add_argument("--top-n", type=int, default=20, help="排行榜保留数量")
    args = parser.parse_args()
    if args.top_n < 1:
        parser.error("--top-n 必须大于 0")
    try:
        ZoneInfo(args.timezone)
    except Exception as error:
        parser.error(f"无效时区 {args.timezone}: {error}")
    return args


def discover_files(root: pathlib.Path) -> list[pathlib.Path]:
    if (root / "creators").is_dir():
        files = sorted((root / "creators").glob("*.json"))
    else:
        files = sorted(root.glob("video_batches_*/creators/*.json"))
    if not files:
        raise FileNotFoundError(f"没有找到作者结果 JSON: {root}")
    return files


def load_author_index(files: Iterable[pathlib.Path]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    authors: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, str]] = []
    for path in files:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("videos"), list):
                raise ValueError("根节点不是有效作者结果")
            uid = str(result.get("uid") or "")
            if not uid:
                raise ValueError("缺少作者 UID")
            author = result.get("author") if isinstance(result.get("author"), dict) else {}
            authors.setdefault(uid, {
                "uid": uid,
                "name": author.get("name") or uid,
                "fans": author.get("fans"),
                "level": author.get("level"),
            })
        except (OSError, ValueError, json.JSONDecodeError) as error:
            invalid.append({"path": str(path), "error": str(error)})
    return authors, invalid


def participant_ids(video: dict[str, Any]) -> tuple[str, set[str], list[dict[str, Any]]]:
    owner = video.get("owner") if isinstance(video.get("owner"), dict) else {}
    owner_uid = str(owner.get("mid") or "")
    staff = [item for item in (video.get("staff") or []) if isinstance(item, dict)]
    participants = {owner_uid} if owner_uid else set()
    participants.update(str(item.get("mid") or "") for item in staff if item.get("mid") not in (None, ""))
    return owner_uid, participants, staff


def video_identity(video: dict[str, Any]) -> dict[str, Any]:
    owner = video.get("owner") if isinstance(video.get("owner"), dict) else {}
    return {
        "bvid": str(video.get("bvid") or ""),
        "title": video.get("title") or "-",
        "owner_uid": str(owner.get("mid") or ""),
        "owner_name": owner.get("name") or str(owner.get("mid") or "-"),
        "pubdate": video.get("pubdate"),
        "view": (video.get("stat") or {}).get("view"),
        "like": (video.get("stat") or {}).get("like"),
    }


def period_keys(timestamp: int, tz: ZoneInfo) -> tuple[str, str, str, datetime]:
    dt = datetime.fromtimestamp(timestamp, tz)
    iso = dt.isocalendar()
    return dt.strftime("%Y-%m"), f"{iso.year}-W{iso.week:02d}", str(dt.year), dt


def rate(part: int | float, total: int | float) -> float | None:
    return part / total if total else None


def analyze(files: list[pathlib.Path], authors: dict[str, dict[str, Any]], tz: ZoneInfo, top_n: int) -> dict[str, Any]:
    known_uids = set(authors)
    seen_success: set[str] = set()
    cooperation_videos: dict[str, dict[str, Any]] = {}
    invalid_files: list[dict[str, str]] = []
    association_relations: Counter[str] = Counter()
    data_issues: Counter[str] = Counter()
    unique_success_videos = 0
    duplicate_success_associations = 0
    failed_video_records = 0
    total_video_records = 0
    noncoop_distributions = {name: Distribution() for name in ("duration", *METRICS)}
    noncoop_metric_totals: Counter[str] = Counter()

    for path in files:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("videos"), list):
                raise ValueError("根节点不是有效作者结果")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            invalid_files.append({"path": str(path), "error": str(error)})
            continue

        for video in result["videos"]:
            if not isinstance(video, dict):
                data_issues["non_object_video_records"] += 1
                continue
            total_video_records += 1
            if video.get("status") != "success":
                failed_video_records += 1
                continue
            association_relations[str(video.get("creator_relation") or "unknown")] += 1
            bvid = str(video.get("bvid") or "")
            if not bvid:
                data_issues["success_without_bvid"] += 1
                continue
            is_cooperation = video.get("is_cooperation") in (True, 1)
            _, _, staff = participant_ids(video)
            if staff and not is_cooperation:
                data_issues["staff_nonempty_but_flag_false"] += 1
            if is_cooperation and not staff:
                data_issues["flag_true_but_staff_empty"] += 1

            if bvid in seen_success:
                duplicate_success_associations += 1
                if is_cooperation and bvid not in cooperation_videos:
                    data_issues["cooperation_flag_inconsistent_across_duplicates"] += 1
                continue
            seen_success.add(bvid)
            unique_success_videos += 1
            if is_cooperation:
                cooperation_videos[bvid] = video
            else:
                stat = video.get("stat") if isinstance(video.get("stat"), dict) else {}
                noncoop_distributions["duration"].add(video.get("duration"))
                for name in METRICS:
                    noncoop_distributions[name].add(stat.get(name))
                    value = stat.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        noncoop_metric_totals[name] += value

    monthly_authors: defaultdict[str, set[str]] = defaultdict(set)
    weekly_authors: defaultdict[str, set[str]] = defaultdict(set)
    yearly_authors: defaultdict[str, set[str]] = defaultdict(set)
    monthly_videos: Counter[str] = Counter()
    yearly_videos: Counter[str] = Counter()
    known_participants: set[str] = set()
    visible_owners: set[str] = set()
    visible_collaborators: set[str] = set()
    known_owner_uids: set[str] = set()
    known_staff_uids: set[str] = set()
    author_video_counts: Counter[str] = Counter()
    author_owner_counts: Counter[str] = Counter()
    author_staff_counts: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    role_counts: Counter[str] = Counter()
    role_members: defaultdict[str, set[str]] = defaultdict(set)
    team_sizes = Distribution()
    known_team_sizes = Distribution()
    coop_distributions = {name: Distribution() for name in ("duration", *METRICS)}
    coop_metric_totals: Counter[str] = Counter()
    dated_videos = 0
    missing_pubdate = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    top_videos: list[dict[str, Any]] = []

    for bvid, video in cooperation_videos.items():
        owner_uid, all_participants, staff = participant_ids(video)
        visible_owners.add(owner_uid)
        collaborators = all_participants - ({owner_uid} if owner_uid else set())
        visible_collaborators.update(collaborators)
        known_for_video = all_participants & known_uids
        known_participants.update(known_for_video)
        if owner_uid in known_uids:
            known_owner_uids.add(owner_uid)
        known_staff = collaborators & known_uids
        known_staff_uids.update(known_staff)
        team_sizes.add(len(collaborators))
        known_team_sizes.add(len(known_for_video))
        if not collaborators:
            data_issues["cooperation_without_real_collaborator"] += 1

        for uid in known_for_video:
            author_video_counts[uid] += 1
            if uid == owner_uid:
                author_owner_counts[uid] += 1
            else:
                author_staff_counts[uid] += 1
        for left, right in itertools.combinations(sorted(known_for_video), 2):
            pair_counts[(left, right)] += 1

        roles_seen: set[tuple[str, str]] = set()
        for item in staff:
            uid = str(item.get("mid") or "")
            if not uid or uid == owner_uid:
                continue
            role = str(item.get("title") or "未标注").strip() or "未标注"
            if (uid, role) in roles_seen:
                continue
            roles_seen.add((uid, role))
            role_counts[role] += 1
            role_members[role].add(uid)

        stat = video.get("stat") if isinstance(video.get("stat"), dict) else {}
        coop_distributions["duration"].add(video.get("duration"))
        for name in METRICS:
            coop_distributions[name].add(stat.get(name))
            value = stat.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                coop_metric_totals[name] += value

        timestamp = video.get("pubdate")
        if isinstance(timestamp, int) and not isinstance(timestamp, bool):
            month, week, year, dt = period_keys(timestamp, tz)
            dated_videos += 1
            earliest = dt if earliest is None else min(earliest, dt)
            latest = dt if latest is None else max(latest, dt)
            monthly_authors[month].update(known_for_video)
            weekly_authors[week].update(known_for_video)
            yearly_authors[year].update(known_for_video)
            monthly_videos[month] += 1
            yearly_videos[year] += 1
        else:
            missing_pubdate += 1
        top_videos.append(video_identity(video))

    coop_summaries = {name: values.summary() for name, values in coop_distributions.items()}
    noncoop_summaries = {name: values.summary() for name, values in noncoop_distributions.items()}
    monthly_counts = {key: len(value) for key, value in sorted(monthly_authors.items())}
    weekly_counts = {key: len(value) for key, value in sorted(weekly_authors.items())}
    yearly_counts = {key: len(value) for key, value in sorted(yearly_authors.items())}

    seen_before: set[str] = set()
    monthly_new: dict[str, int] = {}
    monthly_returning: dict[str, int] = {}
    for month, active in sorted(monthly_authors.items()):
        newcomers = active - seen_before
        monthly_new[month] = len(newcomers)
        monthly_returning[month] = len(active - newcomers)
        seen_before.update(active)

    active_month_counts: Counter[str] = Counter()
    for active in monthly_authors.values():
        active_month_counts.update(active)
    author_frequency = Distribution()
    active_month_frequency = Distribution()
    for uid in known_participants:
        author_frequency.add(author_video_counts[uid])
        active_month_frequency.add(active_month_counts[uid])

    contributions = sorted(author_video_counts.values(), reverse=True)
    contribution_total = sum(contributions)

    def top_share(fraction: float) -> float | None:
        if not contributions or not contribution_total:
            return None
        count = max(1, math.ceil(len(contributions) * fraction))
        return sum(contributions[:count]) / contribution_total

    author_rows = []
    for uid, count in author_video_counts.most_common(top_n):
        profile = authors.get(uid, {})
        author_rows.append({
            "uid": uid,
            "name": profile.get("name") or uid,
            "fans": profile.get("fans"),
            "participated": count,
            "as_owner": author_owner_counts[uid],
            "as_staff": author_staff_counts[uid],
            "active_months": active_month_counts[uid],
        })

    pair_rows = []
    for (left, right), count in pair_counts.most_common(top_n):
        pair_rows.append({
            "left_uid": left,
            "left_name": authors.get(left, {}).get("name") or left,
            "right_uid": right,
            "right_name": authors.get(right, {}).get("name") or right,
            "videos": count,
        })

    role_rows = [
        {"role": role, "records": count, "unique_members": len(role_members[role])}
        for role, count in role_counts.most_common(top_n)
    ]
    top_videos.sort(key=lambda item: numeric(item.get("view")), reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "timezone": str(tz),
        "source": {
            "files": len(files),
            "known_authors": len(known_uids),
            "invalid_files": invalid_files,
            "total_video_records": total_video_records,
            "failed_video_records": failed_video_records,
            "successful_unique_videos": unique_success_videos,
            "duplicate_success_associations": duplicate_success_associations,
            "association_relations": dict(association_relations),
            "data_issues": dict(data_issues),
        },
        "cooperation": {
            "unique_videos": len(cooperation_videos),
            "dated_videos": dated_videos,
            "missing_pubdate": missing_pubdate,
            "unique_video_rate": rate(len(cooperation_videos), unique_success_videos),
            "known_participants": len(known_participants),
            "known_participant_rate": rate(len(known_participants), len(known_uids)),
            "known_owners": len(known_owner_uids),
            "known_staff": len(known_staff_uids),
            "known_both_roles": len(known_owner_uids & known_staff_uids),
            "visible_owners": len(visible_owners - {""}),
            "visible_collaborators": len(visible_collaborators - {""}),
            "date_start": earliest.isoformat() if earliest else None,
            "date_end": latest.isoformat() if latest else None,
            "team_size": team_sizes.summary(),
            "known_participants_per_video": known_team_sizes.summary(),
            "author_video_frequency": author_frequency.summary(),
            "author_active_months": active_month_frequency.summary(),
            "repeat_authors": sum(1 for value in author_video_counts.values() if value >= 2),
            "top_1pct_contribution_share": top_share(0.01),
            "top_10pct_contribution_share": top_share(0.10),
            "metrics": coop_summaries,
            "metric_totals": dict(coop_metric_totals),
        },
        "non_cooperation": {
            "unique_videos": unique_success_videos - len(cooperation_videos),
            "metrics": noncoop_summaries,
            "metric_totals": dict(noncoop_metric_totals),
        },
        "time": {
            "monthly_authors": monthly_counts,
            "weekly_authors": weekly_counts,
            "yearly_authors": yearly_counts,
            "monthly_videos": dict(sorted(monthly_videos.items())),
            "yearly_videos": dict(sorted(yearly_videos.items())),
            "monthly_new_authors": monthly_new,
            "monthly_returning_authors": monthly_returning,
        },
        "roles": role_rows,
        "top_authors": author_rows,
        "top_pairs": pair_rows,
        "top_videos": top_videos[:top_n],
    }


def numeric(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else -math.inf


def fmt_number(value: Any, digits: int = 0) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    if digits:
        return f"{value:,.{digits}f}"
    return f"{value:,.0f}"


def fmt_rate(value: Any) -> str:
    return f"{value:.1%}" if isinstance(value, (int, float)) else "-"


def esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "-"))


def author_link(uid: str, name: str) -> str:
    return f'<a href="https://space.bilibili.com/{html.escape(uid, quote=True)}">{esc(name)}</a>'


def video_link(bvid: str, title: str) -> str:
    return f'<a href="https://www.bilibili.com/video/{html.escape(bvid, quote=True)}">{esc(title)}</a>'


def line_chart(series: dict[str, int], color: str = "#23a6d5", height: int = 330) -> str:
    items = list(series.items())
    if not items:
        return '<p class="empty">无可绘制数据</p>'
    width = 1120
    left, right, top, bottom = 72, 24, 24, 64
    plot_w, plot_h = width - left - right, height - top - bottom
    maximum = max(value for _, value in items) or 1
    points: list[tuple[float, float]] = []
    for index, (_, value) in enumerate(items):
        x = left + (plot_w * index / max(1, len(items) - 1))
        y = top + plot_h - plot_h * value / maximum
        points.append((x, y))
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"{left},{top + plot_h} {polyline} {left + plot_w},{top + plot_h}"
    grid = []
    for step in range(5):
        value = maximum * step / 4
        y = top + plot_h - plot_h * step / 4
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="{left - 10}" y="{y + 5:.1f}" text-anchor="end">{fmt_number(value)}</text>')
    desired_ticks = 12
    step = max(1, math.ceil(len(items) / desired_ticks))
    tick_indexes = list(range(0, len(items), step))
    if tick_indexes[-1] != len(items) - 1:
        tick_indexes.append(len(items) - 1)
    ticks = []
    for index in tick_indexes:
        label = items[index][0]
        x = points[index][0]
        ticks.append(f'<line x1="{x:.1f}" y1="{top + plot_h}" x2="{x:.1f}" y2="{top + plot_h + 6}" class="axis"/>')
        ticks.append(f'<text x="{x:.1f}" y="{top + plot_h + 24}" text-anchor="middle">{esc(label)}</text>')
    peak_index = max(range(len(items)), key=lambda index: items[index][1])
    peak_x, peak_y = points[peak_index]
    peak_label, peak_value = items[peak_index]
    return "".join([
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">',
        *grid,
        f'<polygon points="{area}" fill="{color}" opacity="0.12"/>',
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round"/>',
        *ticks,
        f'<circle cx="{peak_x:.1f}" cy="{peak_y:.1f}" r="5" fill="{color}"/>',
        f'<text x="{peak_x:.1f}" y="{max(15, peak_y - 12):.1f}" text-anchor="middle" class="peak">峰值 {esc(peak_label)}：{peak_value}</text>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" class="axis"/>',
        '</svg>',
    ])


def stacked_line_chart(active: dict[str, int], new: dict[str, int], returning: dict[str, int]) -> str:
    # 主线保留当月全部参与作者，明细表解释首次参与与此前参与过的作者构成。
    chart = line_chart(active, "#7c5cff")
    latest = list(active)[-12:]
    rows = "".join(
        f"<tr><td>{esc(month)}</td><td>{active[month]:,}</td><td>{new.get(month, 0):,}</td>"
        f"<td>{returning.get(month, 0):,}</td></tr>"
        for month in reversed(latest)
    )
    return chart + (
        '<div class="table-wrap"><table><thead><tr><th>月份</th><th>当月参与作者（去重）</th>'
        '<th>历史首次参与作者</th>'
        f'<th>此前已参与过的作者</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


def horizontal_bar_chart(rows: list[tuple[str, float]], color: str = "#ff7a59") -> str:
    rows = rows[:15]
    if not rows:
        return '<p class="empty">无可绘制数据</p>'
    maximum = max(value for _, value in rows) or 1
    bars = []
    for label, value in rows:
        width = 100 * value / maximum
        bars.append(
            '<div class="bar-row">'
            f'<div class="bar-label">{esc(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width:.2f}%;background:{color}"></div></div>'
            f'<div class="bar-value">{fmt_number(value)}</div></div>'
        )
    return '<div class="bars">' + "".join(bars) + '</div>'


def metric_table(coop: dict[str, Any], noncoop: dict[str, Any]) -> str:
    rows = []
    for name in ("duration", *METRICS):
        left, right = coop[name], noncoop[name]
        rows.append(
            f"<tr><td>{METRIC_LABELS[name]}</td><td>{fmt_number(left['median'], 1)}</td>"
            f"<td>{fmt_number(left['p25'], 1)}–{fmt_number(left['p75'], 1)}</td>"
            f"<td>{fmt_number(left['mean'], 1)}</td><td>{fmt_number(right['median'], 1)}</td>"
            f"<td>{fmt_number(right['mean'], 1)}</td></tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>每部作品的指标</th><th>联合作品中位数</th>'
        '<th>联合作品第 25–75 分位</th><th>联合作品均值</th><th>非联合作品中位数</th><th>非联合作品均值</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def generate_html(stats: dict[str, Any], output: pathlib.Path) -> None:
    source = stats["source"]
    coop = stats["cooperation"]
    noncoop = stats["non_cooperation"]
    time = stats["time"]
    team = coop["team_size"]
    frequency = coop["author_video_frequency"]
    active_months = coop["author_active_months"]
    monthly_items = list(time["monthly_authors"].items())
    weekly_items = list(time["weekly_authors"].items())
    monthly_peak = max(monthly_items, key=lambda item: item[1]) if monthly_items else ("-", 0)
    weekly_peak = max(weekly_items, key=lambda item: item[1]) if weekly_items else ("-", 0)

    yearly_rows = "".join(
        f"<tr><td>{year}</td><td>{time['yearly_videos'].get(year, 0):,}</td>"
        f"<td>{count:,}</td></tr>"
        for year, count in time["yearly_authors"].items()
    )
    role_rows = "".join(
        f"<tr><td>{esc(row['role'])}</td><td>{row['records']:,}</td><td>{row['unique_members']:,}</td></tr>"
        for row in stats["roles"]
    )
    author_rows = "".join(
        f"<tr><td>{index}</td><td>{author_link(row['uid'], row['name'])}</td><td>{fmt_number(row['fans'])}</td>"
        f"<td>{row['participated']:,}</td><td>{row['as_owner']:,}</td><td>{row['as_staff']:,}</td>"
        f"<td>{row['active_months']:,}</td></tr>"
        for index, row in enumerate(stats["top_authors"], 1)
    )
    pair_rows = "".join(
        f"<tr><td>{index}</td><td>{author_link(row['left_uid'], row['left_name'])}</td>"
        f"<td>{author_link(row['right_uid'], row['right_name'])}</td><td>{row['videos']:,}</td></tr>"
        for index, row in enumerate(stats["top_pairs"], 1)
    )
    video_rows = "".join(
        f"<tr><td>{index}</td><td>{video_link(row['bvid'], row['title'])}</td><td>{esc(row['owner_name'])}</td>"
        f"<td>{fmt_number(row['view'])}</td><td>{fmt_number(row['like'])}</td></tr>"
        for index, row in enumerate(stats["top_videos"], 1)
    )
    issue_labels = {
        "non_object_video_records": "不是 JSON 对象的作品记录",
        "success_without_bvid": "状态成功但缺少 BV 号的记录",
        "staff_nonempty_but_flag_false": "有 staff 但未标记为联合投稿的关联记录",
        "flag_true_but_staff_empty": "标记为联合投稿但 staff 为空的关联记录",
        "cooperation_flag_inconsistent_across_duplicates": "同一 BV 的重复记录中联合投稿标记不一致",
        "cooperation_without_real_collaborator": "按 UID 排除主投稿者后没有合作成员的作品",
    }
    issue_rows = "".join(
        f"<tr><td>{esc(issue_labels.get(key, key))}</td><td>{value:,}</td></tr>"
        for key, value in source["data_issues"].items()
    ) or '<tr><td>未发现结构异常</td><td>0</td></tr>'
    role_chart = horizontal_bar_chart([(row["role"], row["records"]) for row in stats["roles"]])
    year_chart = horizontal_bar_chart([(key, value) for key, value in time["yearly_authors"].items()], "#23a6d5")

    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bilibili 联合投稿描述性统计</title>
<style>
:root{{--bg:#f5f7fb;--card:#fff;--ink:#172033;--muted:#667085;--line:#e4e8f0;--blue:#23a6d5;--purple:#7c5cff;--orange:#ff7a59}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.hero{{background:linear-gradient(125deg,#17233f,#244b72 58%,#257f9c);color:#fff;padding:54px max(24px,calc((100% - 1180px)/2)) 44px}}
.hero h1{{margin:0 0 10px;font-size:34px;letter-spacing:.02em}} .hero p{{max-width:930px;margin:7px 0;color:#dcecff}}
main{{max-width:1180px;margin:0 auto;padding:28px 20px 64px}} section{{margin:0 0 28px}}
h2{{font-size:24px;margin:0 0 14px}} h3{{font-size:18px;margin:0 0 10px}} .note{{color:var(--muted);margin:4px 0 16px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}} .grid.two{{grid-template-columns:repeat(2,1fr)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:0 8px 28px rgba(23,32,51,.045)}}
.kpi .value{{font-size:30px;font-weight:750;line-height:1.2;margin:8px 0 3px}} .kpi .label{{color:var(--muted)}} .kpi .sub{{font-size:13px;color:var(--muted)}}
.chart{{width:100%;height:auto;overflow:visible}} .chart text{{font-size:12px;fill:#667085}} .chart .grid{{stroke:#e9edf3;stroke-width:1}} .chart .axis{{stroke:#aab3c2;stroke-width:1}} .chart .peak{{font-weight:700;fill:#29364d}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:10px}} table{{border-collapse:collapse;width:100%;background:#fff}} th,td{{padding:10px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}} th{{background:#f8fafc;color:#526075;font-weight:650;position:sticky;top:0}} th:first-child,td:first-child{{text-align:left}} tr:last-child td{{border-bottom:0}} a{{color:#147ca8;text-decoration:none}} a:hover{{text-decoration:underline}}
.bars{{display:grid;gap:9px}} .bar-row{{display:grid;grid-template-columns:120px 1fr 70px;align-items:center;gap:10px}} .bar-label{{text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .bar-track{{height:16px;background:#eef1f6;border-radius:8px;overflow:hidden}} .bar-fill{{height:100%;border-radius:8px}} .bar-value{{font-variant-numeric:tabular-nums}}
.callout{{border-left:4px solid var(--purple);padding:12px 16px;background:#f4f1ff;border-radius:0 9px 9px 0}} .small{{font-size:13px;color:var(--muted)}} ul{{padding-left:22px}} code{{background:#eef1f6;padding:2px 5px;border-radius:4px}}
footer{{color:var(--muted);font-size:13px;margin-top:36px}}
@media(max-width:820px){{.grid,.grid.two{{grid-template-columns:1fr 1fr}}.hero h1{{font-size:28px}}}}
@media(max-width:520px){{.grid,.grid.two{{grid-template-columns:1fr}}.hero{{padding:36px 20px}}main{{padding:20px 12px 48px}}.bar-row{{grid-template-columns:92px 1fr 56px}}}}
</style>
</head>
<body>
<header class="hero">
  <h1>Bilibili 联合投稿描述性统计</h1>
  <p>作者范围为本地 {source['known_authors']:,} 位有作者文件的作者。时间图同时统计主投稿者和合作成员，但不统计这 {source['known_authors']:,} 位作者之外的人。</p>
  <p class="small">生成时间：{esc(stats['generated_at'])}　发布日期时区：{esc(stats['timezone'])}　联合作品日期范围：{esc(coop['date_start'])} 至 {esc(coop['date_end'])}</p>
</header>
<main>
<section>
  <h2>核心概况</h2>
  <div class="grid">
    <div class="card kpi"><div class="label">联合作品数（按 BV 号去重）</div><div class="value">{coop['unique_videos']:,}</div><div class="sub">占成功作品（按 BV 号去重）{fmt_rate(coop['unique_video_rate'])}</div></div>
    <div class="card kpi"><div class="label">5,798 位作者中曾参与联合投稿的人数</div><div class="value">{coop['known_participants']:,}</div><div class="sub">每位作者全期只计一次，占作者范围 {fmt_rate(coop['known_participant_rate'])}</div></div>
    <div class="card kpi"><div class="label">其中曾作为主投稿者的人数</div><div class="value">{coop['known_owners']:,}</div><div class="sub">作品 owner UID 属于 5,798 位作者</div></div>
    <div class="card kpi"><div class="label">其中曾作为合作成员的人数</div><div class="value">{coop['known_staff']:,}</div><div class="sub">staff 去重并排除 owner；两种身份都担任过 {coop['known_both_roles']:,} 人</div></div>
  </div>
  <p class="callout">“参与联合投稿的作者”定义为：成功联合作品的 <code>owner + staff</code>，在每部作品内按 UID 去重，再与 5,798 位作者取交集。主投稿者和合作成员都计入，但作者范围外的人不计入。</p>
</section>

<section>
  <h2>每月参与联合作品的作者数（限 5,798 位作者）</h2>
  <p class="note">每个月内按作者 UID 去重；同一作者当月参与多部作品仍只计 1 人。峰值为 {esc(monthly_peak[0])} 的 {monthly_peak[1]:,} 人。</p>
  <div class="card">{stacked_line_chart(time['monthly_authors'], time['monthly_new_authors'], time['monthly_returning_authors'])}</div>
</section>

<section>
  <h2>每周参与联合作品的作者数（限 5,798 位作者）</h2>
  <p class="note">采用 ISO 周（周一至周日），跨年周归属 ISO 周年。峰值为 {esc(weekly_peak[0])} 的 {weekly_peak[1]:,} 人。</p>
  <div class="card">{line_chart(time['weekly_authors'], '#23a6d5')}</div>
</section>

<section>
  <h2>各年联合作品数与参与作者数</h2>
  <div class="grid two">
    <div class="card"><h3>每年参与作者数（年内按 UID 去重）</h3>{year_chart}</div>
    <div class="card"><h3>年度明细</h3><div class="table-wrap"><table><thead><tr><th>年份</th><th>联合作品（按 BV 去重）</th><th>参与作者（限 5,798 位）</th></tr></thead><tbody>{yearly_rows}</tbody></table></div></div>
  </div>
</section>

<section>
  <h2>团队规模与角色</h2>
  <div class="grid">
    <div class="card kpi"><div class="label">每部作品的合作成员数中位数</div><div class="value">{fmt_number(team['median'], 1)}</div><div class="sub">staff 按 UID 去重并排除主投稿者</div></div>
    <div class="card kpi"><div class="label">每部作品合作成员数的第 75 分位</div><div class="value">{fmt_number(team['p75'], 1)}</div><div class="sub">第 90 分位为 {fmt_number(team['p90'], 1)} 人，同样不含主投稿者</div></div>
    <div class="card kpi"><div class="label">单部作品最多合作成员数</div><div class="value">{fmt_number(team['max'])}</div><div class="sub">staff 按 UID 去重并排除主投稿者</div></div>
    <div class="card kpi"><div class="label">全部联合作品中的合作成员去重数</div><div class="value">{coop['visible_collaborators']:,}</div><div class="sub">不含主投稿者；包含 5,798 位作者之外的人</div></div>
  </div>
  <div class="grid two" style="margin-top:14px">
    <div class="card"><h3>合作成员角色出现次数</h3><p class="small">同一成员在不同作品中以同一角色出现，会分别计数。</p>{role_chart}</div>
    <div class="card"><h3>角色明细</h3><div class="table-wrap"><table><thead><tr><th>角色</th><th>角色出现次数（作品×成员）</th><th>担任过该角色的成员数（按 UID 去重）</th></tr></thead><tbody>{role_rows}</tbody></table></div></div>
  </div>
</section>

<section>
  <h2>作品表现</h2>
  <p class="note">互动指标为抓取时累计值。分布明显右偏，因此以中位数与四分位区间为主，均值仅作补充。对比是描述性结果，不代表联合投稿带来的因果效果。</p>
  <div class="card">{metric_table(coop['metrics'], noncoop['metrics'])}</div>
</section>

<section>
  <h2>作者活跃、持续性与集中度</h2>
  <div class="grid">
    <div class="card kpi"><div class="label">每位参与作者涉及的联合作品数中位数</div><div class="value">{fmt_number(frequency['median'], 1)}</div><div class="sub">每部作品只计一次；第 75 分位为 {fmt_number(frequency['p75'], 1)} 部</div></div>
    <div class="card kpi"><div class="label">参与至少 2 部联合作品的作者数</div><div class="value">{coop['repeat_authors']:,}</div><div class="sub">仅限 5,798 位作者范围</div></div>
    <div class="card kpi"><div class="label">每位参与作者的活跃月数中位数</div><div class="value">{fmt_number(active_months['median'], 1)}</div><div class="sub">一个月内参与多部仍计 1 个活跃月；第 90 分位为 {fmt_number(active_months['p90'], 1)} 个月</div></div>
    <div class="card kpi"><div class="label">参与次数最多的前 10% 作者所占份额</div><div class="value">{fmt_rate(coop['top_10pct_contribution_share'])}</div><div class="sub">分母为所有“作者—联合作品”参与记录；前 1% 占 {fmt_rate(coop['top_1pct_contribution_share'])}</div></div>
  </div>
  <div class="card" style="margin-top:14px"><h3>参与联合作品最多的作者（限 5,798 位作者）</h3><div class="table-wrap"><table><thead><tr><th>排名</th><th>作者</th><th>抓取时粉丝数</th><th>参与联合作品数</th><th>作为主投稿者</th><th>作为合作成员（不含 owner）</th><th>有参与记录的月份数</th></tr></thead><tbody>{author_rows}</tbody></table></div></div>
</section>

<section>
  <h2>重复合作关系</h2>
  <p class="note">仅统计同一部联合作品中同时出现、且都属于 5,798 位已知作者的两两组合。大型团队会产生较多组合，结果应理解为共同出现次数。</p>
  <div class="card"><div class="table-wrap"><table><thead><tr><th>排名</th><th>作者 A</th><th>作者 B</th><th>两人共同参与的联合作品数</th></tr></thead><tbody>{pair_rows}</tbody></table></div></div>
</section>

<section>
  <h2>高播放联合作品</h2>
  <div class="card"><div class="table-wrap"><table><thead><tr><th>排名</th><th>作品</th><th>主投稿者</th><th>播放</th><th>点赞</th></tr></thead><tbody>{video_rows}</tbody></table></div></div>
</section>

<section>
  <h2>数据质量与解释限制</h2>
  <div class="grid two">
    <div class="card"><h3>数据覆盖</h3><ul>
      <li>作者文件：{source['files']:,}；从作者文件取得的作者范围：{source['known_authors']:,} 个 UID。</li>
      <li>原始作品记录：{source['total_video_records']:,}；失败记录：{source['failed_video_records']:,}。</li>
      <li>成功作品按 BV 号去重后：{source['successful_unique_videos']:,} 部；同一 BV 出现在多个作者文件形成的重复关联：{source['duplicate_success_associations']:,} 条。</li>
      <li>联合作品有日期：{coop['dated_videos']:,}；缺少日期：{coop['missing_pubdate']:,}。</li>
      <li>全部联合作品中的主投稿者按 UID 去重后共有 {coop['visible_owners']:,} 人，其中不少不属于 5,798 位作者；时间图没有统计这些范围外作者。</li>
    </ul></div>
    <div class="card"><h3>结构异常</h3><div class="table-wrap"><table><thead><tr><th>检查项</th><th>记录数</th></tr></thead><tbody>{issue_rows}</tbody></table></div></div>
  </div>
  <div class="card" style="margin-top:14px"><h3>解读注意</h3><ul>
    <li>末月和末周可能是不完整期间，不应直接与完整期间比较；图中不对不完整期间做环比或同比判断。</li>
    <li><code>staff</code> 几乎总是包含主投稿者本人；本报告凡称“合作成员”，均已按 UID 去重并排除 owner。</li>
    <li>作品互动量受发布时间、作者体量、内容类型及累计曝光时长影响；联合与非联合投稿的差异仅是描述性关联。</li>
    <li>角色统计覆盖全部作品中可读取的合作成员，包括 5,798 位作者范围外的人；角色名称保留平台原始文本，近义角色未合并。</li>
  </ul></div>
</section>
<footer>由 cooperation_descriptive_analysis.py 基于本地 video_batches_result 生成。HTML 不依赖外部 JavaScript 或图表服务。</footer>
</main>
</body>
</html>
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = pathlib.Path(args.input_dir).expanduser().resolve()
    output = pathlib.Path(args.output).expanduser().resolve()
    try:
        files = discover_files(root)
        authors, index_invalid = load_author_index(files)
        stats = analyze(files, authors, ZoneInfo(args.timezone), args.top_n)
        if index_invalid:
            stats["source"]["author_index_invalid_files"] = index_invalid
        generate_html(stats, output)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"分析失败: {error}")
        return 1
    print(f"已知作者: {stats['source']['known_authors']:,}")
    print(f"唯一联合作品: {stats['cooperation']['unique_videos']:,}")
    print(f"参与联合作品的已知作者: {stats['cooperation']['known_participants']:,}")
    print(f"HTML 报告: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
