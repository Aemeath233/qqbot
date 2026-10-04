"""将用户的实际用电读数绘制为轻量 PNG，无浏览器或公开网页。"""

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from qqbot.commands import SHANGHAI

WIDTH, HEIGHT = 900, 520
INK = (38, 55, 73)
MUTED = (113, 128, 145)
GRID = (223, 231, 238)
BLUE = (52, 120, 246)
FILL = (226, 237, 255)


def render_electricity_chart(rows: list[dict], days: int, path: Path) -> Path:
    """rows 使用 UserStore 的台账记录；不请求校园接口或推测缺失读数。"""
    points = sorted(
        {
            (float(row["observed_at"]), float(row["quantity"]))
            for row in rows
            if not row["cached"]
        }
    )
    if not points:
        raise ValueError("这段时间没有独立电量读数，暂时无法生成曲线图。")
    points = points[-200:]
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    left, right, top, bottom = 86, 34, 45, 70
    plot_width, plot_height = WIDTH - left - right, HEIGHT - top - bottom
    values = [quantity for _, quantity in points]
    low, high = min(values), max(values)
    if low == high:
        margin = max(0.5, abs(low) * 0.05)
        low, high = max(0, low - margin), high + margin
    else:
        margin = (high - low) * 0.1
        low, high = max(0, low - margin), high + margin

    draw.text((left, 13), "Remaining electricity (du)", fill=INK, font=font)
    draw.text((left, 29), f"Last {days} days | {len(points)} readings", fill=MUTED, font=font)

    def y_for(value: float) -> int:
        return round(top + (high - value) / (high - low) * plot_height)

    for index in range(5):
        value = high - (high - low) * index / 4
        y = y_for(value)
        draw.line((left, y, WIDTH - right, y), fill=GRID, width=1)
        draw.text((8, y - 5), f"{value:.2f}", fill=MUTED, font=font)
    start, end = points[0][0], points[-1][0]
    span = max(end - start, 1)
    coords = [
        (round(left + (stamp - start) / span * plot_width), y_for(value))
        for stamp, value in points
    ]
    if len(coords) > 1:
        polygon = [(left, top + plot_height), *coords, (coords[-1][0], top + plot_height)]
        draw.polygon(polygon, fill=FILL)
        draw.line(coords, fill=BLUE, width=4, joint="curve")
    for x, y in coords:
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=BLUE, outline="white", width=2)
    for index in range(3):
        stamp = start + (end - start) * index / 2
        x = left + (stamp - start) / span * plot_width
        label = datetime.fromtimestamp(stamp, SHANGHAI).strftime("%m-%d %H:%M")
        draw.text((round(x - 28), HEIGHT - bottom + 18), label, fill=MUTED, font=font)
    image.save(path, format="PNG", optimize=True)
    return path
