"""デモ用の温度データを投入する。

    uv run python manage.py seed_temps --year-ago      # 直近12時間 + 1年前(画面の確認に最短)
    uv run python manage.py seed_temps                 # 直近12時間だけ
    uv run python manage.py seed_temps --hours 24 --interval 5   # 直近24時間を5分間隔で
    uv run python manage.py seed_temps --clear --year-ago       # 既存レコードを消してから作り直す
"""

import math
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from temps.models import Temp


class Command(BaseCommand):
    help = "温度記録のデモデータを生成する"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=12, help="直近何時間分つくるか(既定 12)")
        parser.add_argument("--interval", type=int, default=10, help="記録間隔 分(既定 10)")
        parser.add_argument("--year-ago", action="store_true", help="1年前の同じ月日時分もつくる")
        parser.add_argument("--missing", type=float, default=0.05, help="1年前でデータを欠損させる割合(既定 0.05)")
        parser.add_argument("--clear", action="store_true", help="既存レコードを全削除する")
        parser.add_argument("--seed", type=int, default=None, help="乱数シード")
        parser.add_argument("--base", type=float, default=26.0, help="現在の平均温度(既定 26.0)")
        parser.add_argument("--amp", type=float, default=5.0, help="温度の変動幅(既定 5.0)")
        parser.add_argument("--year-base", type=float, default=24.0, help="1年前の平均温度(既定 24.0)")

    @transaction.atomic
    def handle(self, *args, **opts):
        rng = random.Random(opts["seed"])
        interval = timedelta(minutes=max(1, opts["interval"]))
        hours = max(1, opts["hours"])
        steps = int(timedelta(hours=hours) / interval)
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        # 記録間隔の区切りに丸める(00:00 起点)
        midnight = now.replace(hour=0, minute=0)
        minutes_since = int((now - midnight).total_seconds() // 60)
        step_minutes = max(1, opts["interval"])
        newest = midnight + timedelta(minutes=minutes_since - (minutes_since % step_minutes))

        if opts["clear"]:
            deleted, _ = Temp.objects.all().delete()
            self.stdout.write(f"既存レコードを {deleted} 件削除しました")

        created = 0
        created += self._fill(rng, Temp.objects, newest, steps, interval, base=opts["base"], amp=opts["amp"])

        if opts["year_ago"]:
            from temps.services import replace_years  # 循環 import 回避

            created += self._fill(
                rng,
                Temp.objects,
                replace_years(newest, -1),
                steps,
                interval,
                base=opts["year_base"],
                amp=opts["amp"],
                missing=opts["missing"],
            )

        self.stdout.write(
            self.style.SUCCESS(f"温度記録を {created} 件作成しました (合計 {Temp.objects.count()} 件)")
        )

    def _fill(self, rng, manager, newest, steps, interval, *, base, amp, missing=0.0):
        """newest から遡って steps 件つくる。同じ dt があれば上書きしない。"""
        existing = set(
            manager.filter(dt__gte=newest - interval * steps, dt__lte=newest).values_list("dt", flat=True)
        )
        rows = []
        for i in range(steps):
            dt = newest - interval * i
            if dt in existing:
                continue
            if missing and rng.random() < missing:
                continue
            # 日中の山なりにノイズを足した、らしい値にする
            phase = (dt.hour * 60 + dt.minute) / 1440.0 * 2 * math.pi
            temp = Decimal(str(base)) + Decimal(str(amp * math.sin(phase - math.pi / 2)))
            temp += Decimal(str(rng.uniform(-0.6, 0.6)))
            rows.append(Temp(dt=dt, temp=temp.quantize(Decimal("0.1"))))

        Temp.objects.bulk_create(rows, ignore_conflicts=True)
        return len(rows)
