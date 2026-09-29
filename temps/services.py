"""ダッシュボード表示用の集計。

画面は「過去 period 時間分の現在」と「1年前の同じ月日時分」を並べて表示する。
1年前側は必ずしも同じ分にデータが存在するとは限らないため、
対象時刻からの前方探索(指定許容範囲内)で最も近い計測値を対応づける。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .models import Temp

# 画面・API の既定値
DEFAULT_HOURS = 12
DEFAULT_COUNT = 72
# 1年前レコードの対応づけに許容する時刻ズレ
ONE_YEAR_TOLERANCE = timedelta(minutes=30)
# レコード名
LABEL_CURRENT = "現在"
LABEL_ONE_YEAR_AGO = "1年前"


def replace_years(dt: datetime, years: int) -> datetime:
    """同じ月日時分を years 年前後にずらす(2/29 は 2/28 に丸める)。"""
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:  # うるう年
        return dt.replace(year=dt.year + years, day=28)


@dataclass(frozen=True)
class Point:
    dt: datetime
    temp: float


@dataclass(frozen=True)
class Table:
    """テーブル1つぶんの表示データ。"""

    label: str
    rows: list[dict]
    max_index: int | None
    min_index: int | None

    @property
    def max_row(self) -> dict | None:
        return None if self.max_index is None else self.rows[self.max_index]

    @property
    def min_row(self) -> dict | None:
        return None if self.min_index is None else self.rows[self.min_index]

    @property
    def count(self) -> int:
        return len(self.rows)


def _rows_from(points: list[Point]) -> list[dict]:
    """新しい順に並べる(画面のテーブルは新しいものが一番上)。"""
    rows = []
    for p in sorted(points, key=lambda p: p.dt, reverse=True):
        rows.append(
            {
                "dt": p.dt,
                "temp": p.temp,
                # 画面表示用 "26/09/29 16:16"(ロケールに左右されないよう自作書式)
                "dt_text": p.dt.strftime("%y/%m/%d %H:%M"),
            }
        )
    return rows


def _table(label: str, points: list[Point]) -> Table:
    rows = _rows_from(points)
    max_index: int | None = None
    min_index: int | None = None
    if rows:
        temps = [r["temp"] for r in rows]
        max_index = temps.index(max(temps))
        min_index = temps.index(min(temps))
    return Table(label=label, rows=rows, max_index=max_index, min_index=min_index)


def _to_point(row) -> Point:
    return Point(dt=row["dt"], temp=float(row["temp"]))


def load_window(start: datetime, end: datetime) -> list[Point]:
    """start 以上 end 未満の計測値を新しい順に返す。"""
    rows = (
        Temp.objects.filter(dt__gte=start, dt__lt=end)
        .order_by("-dt")
        .values("dt", "temp")
    )
    return [_to_point(r) for r in rows]


def load_year_ago_window(current: list[Point], tolerance: timedelta = ONE_YEAR_TOLERANCE) -> list[Point]:
    """現在の各時刻に対応する 1年前の計測値を返す。

    対応する分にデータが無い場合は、その時刻から tolerance 以内で最も近いものを使う。
    現在のレコード数が count 個に満たない場合は、1年前側の時間窓に収まる範囲で補う。
    """
    if not current:
        return []

    points: dict[Point, Point] = {}
    year_ago_times = [replace_years(p.dt, -1) for p in current]
    earliest = min(year_ago_times) - tolerance
    latest = max(year_ago_times) + tolerance
    pool = list(
        Temp.objects.filter(dt__gte=earliest, dt__lte=latest)
        .order_by("dt")
        .values_list("dt", "temp", flat=False)
    )
    pool = [Point(dt=dt, temp=float(temp)) for dt, temp in pool]
    if not pool:
        return []

    for target in year_ago_times:
        best: Point | None = None
        best_gap: timedelta | None = None
        for cand in pool:
            gap = abs(cand.dt - target)
            if gap <= tolerance and (best_gap is None or gap < best_gap):
                best, best_gap = cand, gap
        if best is not None and best not in points:
            points[best] = best

    result = list(points.keys())
    if len(result) < len(current):
        # 1年前側の窓に含まれる実データを補充する(既に採ったものと時刻が近いものは除外)
        taken = {p.dt for p in result}
        for cand in pool:
            if len(result) >= len(current):
                break
            if cand.dt in taken:
                continue
            if any(abs(cand.dt - t) <= timedelta(minutes=1) for t in taken):
                continue
            result.append(cand)
            taken.add(cand.dt)

    result.sort(key=lambda p: p.dt, reverse=True)
    return result


def build_dashboard(hours: int = DEFAULT_HOURS, count: int = DEFAULT_COUNT) -> dict:
    """画面表示に必要なデータ一式を組み立てる。"""
    end = timezone.localtime(timezone.now()) + timedelta(minutes=1)
    start = end - timedelta(hours=hours)

    current = load_window(start, end)[:count]
    year_ago = load_year_ago_window(current)

    table_current = _table(LABEL_CURRENT, current)
    table_year_ago = _table(LABEL_ONE_YEAR_AGO, year_ago)

    return {
        "generated_at": end,
        "hours": hours,
        "count": count,
        "start": start,
        "end": end,
        "table_current": table_current,
        "table_year_ago": table_year_ago,
        "series_current": _rows_from(current),
        "series_year_ago": _rows_from(year_ago),
    }
