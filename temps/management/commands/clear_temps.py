"""温度記録 (temps テーブル) をクリアする。

    uv run python manage.py clear_temps                     # 全件削除 (確認してから削除)
    uv run python manage.py clear_temps --yes               # 確認せず全件削除 (スクリプト/cron 向け)
    uv run python manage.py clear_temps --days 30           # 30日より前だけを削除
    uv run python manage.py clear_temps --before "2026-09-01 00:00" --after "2026-01-01"
    uv run python manage.py clear_temps --year-ago          # 1年前の同じ月日時分まわりを削除
    uv run python manage.py clear_temps --dry-run           # 削除せずに対象件数だけ確認
    uv run python manage.py clear_temps --backup dump.json  # 削除対象を JSON に退避してから削除

削除するのはレコードだけ。db.sqlite3 とマイグレーション履歴は残るので画面はそのまま動く。
"""

import sys
from datetime import timedelta
from pathlib import Path

from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from temps.models import Temp
from temps.services import replace_years
from temps.views import _parse_dt  # API と同じ日時パーサ (/ 区切り可, TZ 指定が無ければ Asia/Tokyo)

CONFIRM_WORDS = {"y", "yes", "はい"}


class Command(BaseCommand):
    help = "温度記録 (temps テーブル) をクリアする"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="N日より前を削除する (--before の代わり)")
        parser.add_argument("--before", default=None, help="この日時より前を削除 (YYYY-MM-DD [HH:MM[:SS]])")
        parser.add_argument("--after", default=None, help="この日時より前は残す (--before / --days と併用して期間指定)")
        parser.add_argument(
            "--year-ago", action="store_true", help="1年前の同じ月日時分まわりだけを削除 (seed_temps --year-ago の逆)"
        )
        parser.add_argument("--years", type=int, default=1, help="--year-ago の何年前か (既定 1)")
        parser.add_argument("--dry-run", action="store_true", help="削除せずに対象件数だけを報告する")
        parser.add_argument("--backup", default=None, metavar="PATH", help="削除対象を削除前に JSON に保存する")
        parser.add_argument("-y", "--yes", action="store_true", help="確認プロンプトを省略する")

    def handle(self, *args, **opts):
        total = Temp.objects.count()
        qs, scope = self._target(opts)
        count = qs.count()

        self.stdout.write(f"削除対象: {count} 件 / 全 {total} 件 ({scope})")
        if count == 0:
            self.stdout.write(self.style.SUCCESS("削除するレコードはありません"))
            return

        if opts["dry_run"]:
            first = qs.order_by("dt").first()
            last = qs.order_by("dt").last()
            self.stdout.write(
                f"  --dry-run のため削除しませんでした (範囲 {first.dt:%Y-%m-%d %H:%M} 〜 {last.dt:%Y-%m-%d %H:%M})"
            )
            return

        if not self._confirm(count, opts):
            self.stdout.write("中止しました")
            return

        if opts["backup"]:
            self._dump(qs, opts["backup"])

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"温度記録を {deleted} 件削除しました (残り {Temp.objects.count()} 件)"))

    # --- 削除対象の絞り込み --------------------------------------------

    def _target(self, opts):
        """削除対象の QuerySet と、人間向けの期間説明を返す。"""
        before = self._parse(opts["before"], "--before")
        after = self._parse(opts["after"], "--after")

        if opts["days"] is not None:
            if opts["days"] < 1:
                raise CommandError("--days は 1 以上を指定してください")
            if before is None:  # --before を直接指定したときはそちらを優先
                before = timezone.localtime(timezone.now()) - timedelta(days=opts["days"])
        if before is not None and after is not None and after >= before:
            raise CommandError("--after は --before より前にしてください")

        qs = Temp.objects.all()
        window = None
        if before is not None and after is not None:
            qs = qs.filter(dt__gte=after, dt__lt=before)
            window = f"{after:%Y-%m-%d %H:%M} 以上 {before:%Y-%m-%d %H:%M} 未満"
        elif before is not None:
            qs = qs.filter(dt__lt=before)
            window = f"{before:%Y-%m-%d %H:%M} 未満"
        elif after is not None:
            qs = qs.filter(dt__gte=after)
            window = f"{after:%Y-%m-%d %H:%M} 以上"

        scopes = [window] if window else []
        if opts["year_ago"]:
            years = opts["years"]
            if years < 1:
                raise CommandError("--years は 1 以上を指定してください")
            anchor = replace_years(timezone.localtime(timezone.now()), -years)
            half = timedelta(days=1)
            qs = qs.filter(dt__gte=anchor - half, dt__lte=anchor + half)
            scopes.append(f"{years} 年前 ({anchor:%Y-%m-%d}) のまわり ±1日")

        return qs, " AND ".join(scopes) if scopes else "全期間"

    def _parse(self, value, name):
        if value is None or str(value).strip() == "":
            return None
        try:
            return _parse_dt(value)
        except ValueError as exc:
            raise CommandError(f"{name}: {exc}")

    # --- 確認と退避 -----------------------------------------------------

    def _confirm(self, count, opts):
        if opts["yes"]:
            return True
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            raise CommandError("確認できないので削除しません。消してよいなら --yes を付けてください")
        self.stdout.write(f"{count} 件を削除します。よろしいですか? [y/N]: ", ending="")
        self.stdout.flush()
        return stdin.readline().strip().lower() in CONFIRM_WORDS

    def _dump(self, qs, path):
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(serializers.serialize("json", qs, indent=2), encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"バックアップを書けませんでした: {exc}")
        self.stdout.write(f"削除対象を {target} に保存しました (復元: manage.py loaddata {target})")
