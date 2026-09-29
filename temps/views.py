from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import Temp
from .services import (
    DEFAULT_COUNT,
    DEFAULT_HOURS,
    LABEL_CURRENT,
    LABEL_ONE_YEAR_AGO,
    ONE_YEAR_TOLERANCE,
    build_dashboard,
    replace_years,
)

# 入力として受けつける日時フォーマット(ローカルタイム = Asia/Tokyo)
DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
)


def _parse_dt(value) -> datetime:
    """日時文字列をローカルタイムの aware datetime に変換する。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("/", "-")
        if not text:
            raise ValueError("dt が空です")
        if text.endswith("Z"):
            text = text[:-1]
        dt = None
        for fmt in DT_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"dt を解析できません: {value!r}")
    # 記録単位は分まで。秒・ミリ秒付きで送られても同じ dt として扱う(重複登録防止)
    dt = dt.replace(microsecond=0)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _parse_temp(value) -> Decimal:
    if value is None or str(value).strip() == "":
        raise ValueError("temp が空です")
    try:
        temp = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"temp を数値に変換できません: {value!r}")
    temp = temp.quantize(Decimal("0.1"))
    if abs(temp) > Decimal("999.9"):
        raise ValueError("temp は -999.9 〜 999.9 の範囲で指定してください")
    return temp


def _int_param(params, name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} は数値で指定してください")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} は {minimum} 〜 {maximum} の範囲で指定してください")
    return value


def _serialize(row: dict) -> dict:
    return {
        "dt": timezone.localtime(row["dt"]).strftime("%Y-%m-%d %H:%M:%S"),
        "temp": float(row["temp"]),
    }


# --- 画面 ---------------------------------------------------------------

AUTO_REFRESH_SECONDS = 300


@require_GET
def dashboard(request):
    """デフォルト表示の 1 画面。"""
    try:
        hours = _int_param(request.GET, "hours", DEFAULT_HOURS, minimum=1, maximum=24 * 31)
        count = _int_param(request.GET, "count", DEFAULT_COUNT, minimum=1, maximum=2000)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    data = build_dashboard(hours=hours, count=count)
    return render(
        request,
        "temps/dashboard.html",
        {
            **data,
            "label_current": LABEL_CURRENT,
            "label_one_year_ago": LABEL_ONE_YEAR_AGO,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
            "series_json": json.dumps(
                {
                    "generated_at": data["generated_at"].strftime("%Y-%m-%d %H:%M:%S"),
                    "hours": data["hours"],
                    "count": data["count"],
                    "labels": LABEL_CURRENT,
                    "current": [_serialize(r) for r in data["series_current"]],
                    "year_ago": [_serialize(r) for r in data["series_year_ago"]],
                },
                ensure_ascii=False,
            ),
        },
    )


# --- API: Create(記録用) -------------------------------------------------

@csrf_exempt
@require_http_methods(["POST"])
def create_temp(request):
    """{"dt": "2026-09-29 14:16:00", "temp": "29.1"} を 1 件登録する。

    dt は一意キー。同じ dt で再送された場合は上書き(再送に耐える)。
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return JsonResponse({"ok": False, "error": f"JSON を解析できません: {exc}"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"ok": False, "error": "JSON オブジェクトを送ってください"}, status=400)

    try:
        dt = _parse_dt(body.get("dt")) if body.get("dt") not in (None, "") else timezone.localtime(timezone.now())
        temp = _parse_temp(body.get("temp"))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    obj, created = Temp.objects.update_or_create(dt=dt, defaults={"temp": temp})
    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "id": obj.id,
            "dt": timezone.localtime(obj.dt).strftime("%Y-%m-%d %H:%M:%S"),
            "temp": float(obj.temp),
        },
        status=201 if created else 200,
    )


# --- API: Read(取得用) ---------------------------------------------------

@require_GET
def list_temps(request):
    """count 件(既定 72 件)を新しい順に返す。

    /api/temps/?count=72&hours=12        ... 過去 12 時間から最大 72 件
    /api/temps/?count=100&start=...&end=...  期間指定
    /api/temps/?count=100&year_ago=1&hours=12  1年前の同じ月日時分まわり
    """
    params = request.GET
    try:
        count = _int_param(params, "count", DEFAULT_COUNT, minimum=1, maximum=5000)
        hours = params.get("hours")
        start_raw = params.get("start")
        end_raw = params.get("end")
        year_ago = str(params.get("year_ago", "")).lower() in {"1", "true", "yes"}
        ascending = str(params.get("order", "")).lower() in {"asc", "ascending"}
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    qs = Temp.objects.all()
    try:
        start = _parse_dt(start_raw) if start_raw else None
        end = _parse_dt(end_raw) if end_raw else None
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    # 既定は「新しい順に count 件」だけ。hours / start / end を指定したときだけ期間で絞る。
    if start is None and end is None and hours is not None and str(hours).strip() != "":
        try:
            hours_val = _int_param(params, "hours", DEFAULT_HOURS, minimum=1, maximum=24 * 366)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        end = timezone.localtime(timezone.now()) + timedelta(minutes=1)
        start = end - timedelta(hours=hours_val)

    if year_ago:
        # 期間を 1 年前の同じ月日時分に読み替える。うるう年と対応付けの許容ズレぶん余白を取る。
        if start is None or end is None:
            return JsonResponse(
                {"ok": False, "error": "year_ago を使う場合は期間(hours または start/end)を指定してください"},
                status=400,
            )
        pad = timedelta(days=2) + ONE_YEAR_TOLERANCE
        window = (replace_years(start, -1) - pad, replace_years(end, -1) + pad)
    else:
        window = (start, end)

    if window[0] is not None:
        qs = qs.filter(dt__gte=window[0])
    if window[1] is not None:
        qs = qs.filter(dt__lt=window[1])

    rows = list(qs.order_by("dt" if ascending else "-dt").values("dt", "temp")[:count])

    return JsonResponse(
        {
            "ok": True,
            "count": len(rows),
            "requested": count,
            "year_ago": year_ago,
            # 実際に絞り込みに使った期間(1年前側は年だけ表示される)
            "start": window[0].strftime("%Y-%m-%d %H:%M:%S") if window[0] else None,
            "end": window[1].strftime("%Y-%m-%d %H:%M:%S") if window[1] else None,
            "results": [_serialize(r) for r in rows],
        },
    )


@require_GET
def dashboard_api(request):
    """画面と同じ内容の JSON。リロードなしで更新するために使う。"""
    try:
        hours = _int_param(request.GET, "hours", DEFAULT_HOURS, minimum=1, maximum=24 * 31)
        count = _int_param(request.GET, "count", DEFAULT_COUNT, minimum=1, maximum=2000)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    data = build_dashboard(hours=hours, count=count)
    return JsonResponse(
        {
            "ok": True,
            "generated_at": data["generated_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "hours": data["hours"],
            "count": data["count"],
            "current": [_serialize(r) for r in data["series_current"]],
            "year_ago": [_serialize(r) for r in data["series_year_ago"]],
        }
    )


@require_GET
def health(request):
    latest = Temp.objects.order_by("-dt").first()
    return JsonResponse(
        {
            "ok": True,
            "records": Temp.objects.count(),
            "latest_dt": timezone.localtime(latest.dt).strftime("%Y-%m-%d %H:%M:%S") if latest else None,
            "server_time": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
