"""根据描述性统计 JSON 生成 Markdown 报告和 SVG 图表。"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
from datetime import datetime, timezone
from typing import Any


STATUS_LABELS = {
    "success": "成功",
    "partial_failed": "部分失败",
    "failed": "失败",
    "unknown": "未知",
}
AI_LABELS = {
    "platform_labeled": "平台标注含 AI 内容",
    "author_declared": "作者声明使用 AI",
    "suspected": "平台提示疑似 AI",
    "other_ai_notice": "其他 AI 提示",
}
METRIC_LABELS = {
    "fans": "粉丝数",
    "like_num": "作者累计获赞",
    "archive_count": "账号投稿总数",
    "article_count": "专栏总数",
    "selected_video_records": "入选作品数",
    "duration": "时长（秒）",
    "view": "播放",
    "like": "点赞",
    "coin": "投币",
    "favorite": "收藏",
    "share": "分享",
    "danmaku": "弹幕",
    "reply": "评论",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 Bilibili 作者与作品 Markdown 分析报告")
    parser.add_argument("--stats", default="video_batches_descriptive_stats.json", help="统计 JSON")
    parser.add_argument("--output-dir", default="reports/video_batches_report", help="报告目录")
    parser.add_argument("--title", default="Bilibili 作者与作品数据分析报告", help="报告标题")
    return parser.parse_args()


def format_number(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.2f}万"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def format_rate(value: Any) -> str:
    return f"{value:.2%}" if isinstance(value, (int, float)) else "-"


def markdown_text(value: Any) -> str:
    return (str(value or "-").replace("|", "\\|").replace("[", "\\[")
            .replace("]", "\\]").replace("\n", " "))


def https_url(value: Any) -> str:
    url = str(value or "")
    return "https://" + url[7:] if url.startswith("http://") else url


def online_image(url: Any, width: int, alt: str) -> str:
    source = https_url(url)
    if not source:
        return "-"
    return f'<img src="{html.escape(source, quote=True)}" width="{width}" alt="{html.escape(alt)}">'


def chart_data(values: dict[str, Any], labels: dict[str, str] | None = None) -> list[tuple[str, float]]:
    labels = labels or {}
    return [
        (labels.get(str(key), str(key)), float(value))
        for key, value in values.items()
        if isinstance(value, (int, float))
    ]


def write_bar_chart(
    path: pathlib.Path,
    title: str,
    values: list[tuple[str, float]],
    color: str = "#00AEEC",
) -> bool:
    if not values or max((value for _, value in values), default=0) <= 0:
        return False
    width, row_height, top, bottom = 1000, 42, 72, 35
    label_width, value_width = 185, 125
    plot_width = width - label_width - value_width - 45
    height = top + bottom + row_height * len(values)
    maximum = max(value for _, value in values)
    rows = []
    for index, (label, value) in enumerate(values):
        y = top + index * row_height
        bar_width = max(1, plot_width * value / maximum)
        rows.extend([
            f'<text x="{label_width - 12}" y="{y + 23}" text-anchor="end">{html.escape(label)}</text>',
            f'<rect x="{label_width}" y="{y + 5}" width="{bar_width:.1f}" height="25" rx="3" fill="{color}"/>',
            f'<text x="{label_width + bar_width + 9}" y="{y + 23}">{html.escape(format_number(value))}</text>',
        ])
    svg = "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",sans-serif;fill:#18191c;font-size:15px}</style>',
        f'<text x="24" y="38" font-size="22" font-weight="700">{html.escape(title)}</text>',
        *rows,
        '</svg>',
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg + "\n", encoding="utf-8")
    return True


def metric_table(metrics: dict[str, Any]) -> str:
    lines = ["| 指标 | 有效数 | 缺失 | 最小值 | 最大值 | 平均值 | 标准差 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, values in metrics.items():
        lines.append(
            f"| {METRIC_LABELS.get(name, name)} | {format_number(values.get('count'))} | "
            f"{format_number(values.get('missing'))} | {format_number(values.get('min'))} | "
            f"{format_number(values.get('max'))} | {format_number(values.get('mean'))} | "
            f"{format_number(values.get('std_dev'))} |"
        )
    return "\n".join(lines)


def author_ranking(items: list[dict[str, Any]], value_label: str) -> str:
    lines = [f"| 排名 | 头像 | 作者 | UID | {value_label} |", "| ---: | --- | --- | --- | ---: |"]
    for rank, item in enumerate(items, 1):
        name = markdown_text(item.get("name"))
        url = https_url(item.get("space_url"))
        linked_name = f"[{name}]({url})" if url else name
        lines.append(
            f"| {rank} | {online_image(item.get('face'), 48, name)} | {linked_name} | "
            f"{markdown_text(item.get('uid'))} | {format_number(item.get('value'))} |"
        )
    return "\n".join(lines)


def video_ranking(items: list[dict[str, Any]], value_label: str, show_notice: bool = False) -> str:
    headers = f"| 排名 | 在线封面 | 作品 | 作者 | {value_label}"
    separator = "| ---: | --- | --- | --- | ---:"
    if show_notice:
        headers += " | AI 提示"
        separator += " | ---"
    lines = [headers + " |", separator + " |"]
    for rank, item in enumerate(items, 1):
        title = markdown_text(item.get("title"))
        url = https_url(item.get("url"))
        linked_title = f"[{title}]({url})" if url else title
        creator = markdown_text(item.get("creator_name") or item.get("creator_uid"))
        row = (
            f"| {rank} | {online_image(item.get('pic'), 160, title)} | {linked_title} | "
            f"{creator} | {format_number(item.get('value'))}"
        )
        if show_notice:
            row += f" | {markdown_text(item.get('ai_notice'))}"
        lines.append(row + " |")
    return "\n".join(lines)


def generate_report(stats: dict[str, Any], output_dir: pathlib.Path, title: str) -> pathlib.Path:
    authors, videos = stats["authors"], stats["videos"]
    cooperation, ai = stats["cooperation"], stats["ai_usage"]
    charts_dir = output_dir / "assets"
    charts = {
        "author_status": ("作者抓取状态", chart_data(authors["status_counts"], STATUS_LABELS), "#00AEEC"),
        "author_level": ("作者等级分布", chart_data(authors["level_counts"]), "#FB7299"),
        "author_fans": ("作者粉丝量级分布", chart_data(authors["distributions"]["fans"]), "#6C8EEF"),
        "video_status": ("作品抓取状态", chart_data(videos["status_counts"], STATUS_LABELS), "#00AEEC"),
        "video_views": ("作品播放量级分布", chart_data(videos["distributions"]["views"]), "#6C8EEF"),
        "video_duration": ("作品时长分布", chart_data(videos["distributions"]["duration"]), "#7BCFA6"),
        "publish_year": ("作品发布年份分布", chart_data(videos["publish_time"]["year_counts"]), "#F6BD60"),
        "cooperation_author_share": ("参与联合投稿作者占比", [
            ("参与联合投稿", float(cooperation["authors"])),
            ("其他作者", float(authors["records"] - cooperation["authors"])),
        ], "#00AEEC"),
        "cooperation_video_share": ("联合投稿作品占比（按 BV 号去重）", [
            ("联合投稿", float(cooperation["unique_videos"])),
            ("其他作品", float(cooperation["successful_unique_videos"] - cooperation["unique_videos"])),
        ], "#6C8EEF"),
        "ai_share": ("AI 相关作品占比", [
            ("AI 相关", float(ai["videos"])),
            ("非 AI", float(videos["status_counts"].get("success", 0) - ai["videos"])),
        ], "#A779E9"),
        "ai_category": ("AI 使用标注类型", chart_data(ai["category_counts"], AI_LABELS), "#FB7299"),
        "ai_year": ("AI 相关作品年份分布", chart_data(ai["year_counts"]), "#A779E9"),
        "video_errors": ("作品失败原因", chart_data(videos["error_counts"]), "#E85C5C"),
    }
    available_charts: set[str] = set()
    for name, (chart_title, values, color) in charts.items():
        if write_bar_chart(charts_dir / f"{name}.svg", chart_title, values, color):
            available_charts.add(name)

    filter_info = stats["source"].get("filter") or {}
    since_year = filter_info.get("since_year")
    scope = f"{since_year} 年及以后发布的作品" if since_year else "全部年份作品"
    lines = [
        f"# {title}",
        "",
        f"> 统计范围：{scope}；统计生成时间：{stats.get('generated_at', '-')}。",
        "> 排行榜头像和封面直接引用在线地址，离线阅读时可能无法显示。",
        "",
        "## 1. 核心概况",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 入选作者 | {format_number(authors['records'])} |",
        f"| 作者资料可用率 | {format_rate(authors['profile_availability_rate'])} |",
        f"| 认证作者 | {format_number(authors['certified_count'])} |",
        f"| 作品记录 | {format_number(videos['records'])} |",
        f"| 唯一 BV 号 | {format_number(videos['unique_bvids'])} |",
        f"| 作品抓取成功率 | {format_rate(videos['success_rate'])} |",
        f"| 参与联合投稿作者 | {format_number(cooperation['authors'])} |",
        f"| 参与联合投稿作者占比 | {format_rate(cooperation['author_rate'])} |",
        f"| 联合投稿作品（唯一 BV） | {format_number(cooperation['unique_videos'])} |",
        f"| AI 相关作品 | {format_number(ai['videos'])} |",
        f"| AI 相关作品占比 | {format_rate(ai['video_rate'])} |",
        f"| 涉及 AI 作品的作者 | {format_number(ai['authors'])} |",
        "",
        "## 2. 作者分析",
        "",
    ]
    for name in ("author_status", "author_level", "author_fans"):
        if name in available_charts:
            lines.extend([f"![{charts[name][0]}](assets/{name}.svg)", ""])
    lines.extend([
        "### 作者指标",
        "",
        metric_table(authors["metrics"]),
        "",
        "### 粉丝数排行榜",
        "",
        author_ranking(authors["top_by_fans"], "粉丝数"),
        "",
        "### 累计获赞排行榜",
        "",
        author_ranking(authors["top_by_like_num"], "累计获赞"),
        "",
        "## 3. 作品分析",
        "",
    ])
    for name in ("video_status", "video_views", "video_duration", "publish_year", "video_errors"):
        if name in available_charts:
            lines.extend([f"![{charts[name][0]}](assets/{name}.svg)", ""])
    lines.extend([
        "### 作品指标",
        "",
        metric_table(videos["metrics"]),
        "",
        "### 播放量排行榜",
        "",
        video_ranking(videos["top_by_views"], "播放量"),
        "",
        "### 点赞量排行榜",
        "",
        video_ranking(videos["top_by_likes"], "点赞量"),
        "",
        "## 4. 联合投稿分析",
        "",
        "参与联合投稿作者指当前统计范围内，至少有一条成功作品被标记为 `is_cooperation` 的作者；作品数及排行榜按 BV 号去重。",
        "",
    ])
    for name in ("cooperation_author_share", "cooperation_video_share"):
        if name in available_charts:
            lines.extend([f"![{charts[name][0]}](assets/{name}.svg)", ""])
    lines.extend([
        "### 联合投稿作品播放量排行榜",
        "",
        video_ranking(cooperation["top_by_views"], "播放量"),
        "",
        "### 联合投稿作品点赞量排行榜",
        "",
        video_ranking(cooperation["top_by_likes"], "点赞量"),
        "",
        "## 5. AI 使用分析",
        "",
        "AI 相关作品根据 `argue_info.argue_msg` 中出现 `AI` 或“人工智能”识别。该字段是平台或作者提示，不能替代对视频内容本身的模型识别。",
        "",
    ])
    for name in ("ai_share", "ai_category", "ai_year"):
        if name in available_charts:
            lines.extend([f"![{charts[name][0]}](assets/{name}.svg)", ""])
    lines.extend([
        "### AI 与非 AI 作品平均指标",
        "",
        "| 指标 | AI 作品平均值 | 非 AI 作品平均值 |",
        "| --- | ---: | ---: |",
    ])
    for name in ("duration", "view", "like", "coin", "favorite", "share", "danmaku", "reply"):
        lines.append(
            f"| {METRIC_LABELS[name]} | {format_number(ai['metrics']['ai'][name]['mean'])} | "
            f"{format_number(ai['metrics']['non_ai'][name]['mean'])} |"
        )
    lines.extend([
        "",
        "### AI 相关作品播放量排行榜",
        "",
        video_ranking(ai["top_by_views"], "播放量", show_notice=True),
        "",
        "## 6. 数据口径",
        "",
        "- 年份使用作品 `pubdate`，按 Asia/Shanghai 时区计算；`--since-year` 包含给定年份。",
        "- 指定年份后，只保留至少有一条符合年份条件作品的作者。作者粉丝、累计获赞和账号投稿数仍是抓取时的账号总量。",
        "- 指定年份时，没有 `pubdate` 的失败作品无法判断年份，因此不进入期间统计。",
        "- 跨作者重复 BV 号保留为作者—作品关联，`unique_bvids` 则按 BV 号去重。",
        "- 联合投稿作者按作者 UID 去重；联合投稿作品统计和排行榜按 BV 号去重，避免同一作品因出现在多个参与作者文件中而重复计算。",
        "- 排行榜图片为在线资源，远程地址失效或受防盗链限制时不会显示，但统计数值不受影响。",
        "",
    ])
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    stats_path = pathlib.Path(args.stats).expanduser().resolve()
    output_dir = pathlib.Path(args.output_dir).expanduser().resolve()
    try:
        with stats_path.open(encoding="utf-8") as file:
            stats = json.load(file)
        if not all(key in stats for key in ("authors", "videos", "cooperation", "ai_usage")):
            raise ValueError("统计 JSON 缺少 authors、videos、cooperation 或 ai_usage")
        report = generate_report(stats, output_dir, args.title)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as error:
        print(f"报告生成失败: {error}")
        return 1
    print(f"报告已生成: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
