"""CSV から温度記録 (temps テーブル) をインポートする。

    uv run python manage.py seed_temps_csv                     # docs/pi.csv を取り込む
    uv run python manage.py seed_temps_csv path/to/file.csv    # ファイルを指定する
    uv run python manage.py seed_temps_csv --dry-run           # 書き込まずに対象だけ確認する
    uv run python manage.py seed_temps_csv --clear             # 既存レコードを全削除してから取り込む
    uv run python manage.py seed_temps_csv --skip-existing     # 既存の dt は触らない
    uv run python manage.py seed_temps_csv --since 2026-09-01  # 期間を絞る

読み取るのは 1 列目(記録日時)と 2 列目(温度)だけ。それ以外の列とヘッダ行は無視する。
日時はプロジェクトのローカルタイム (Asia/Tokyo) として解釈し、秒まで保持する。
同じ日時が複数行ある場合は、後の行の温度を採用する。
"""

import csv
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from temps.models import Temp
from temps.views import _parse_dt, _parse_temp

# 既定で読むファイル(リポジトリ直下の docs/pi.csv)
DEFAULT_CSV = "docs/pi.csv"
# 1 バッチで INSERT / IN 検索する件数 (SQLite の変数数上限に配慮)
BATCH = 500
# 時時に読むのをやめる行数 (誤指定の暴走防止)
MAX_ROWS = 5_000_000


class Command(BaseCommand):
    help = "CSV (1列目=日時, 2列目=温度) から温度記録をインポートする"

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default=None, help=f"CSV のパス (既定 {DEFAULT_CSV})")
        parser.add_argument("--encoding", default="utf-8-sig", help="CSV のエンコーディング (既定 utf-8-sig)")
        parser.add_argument("--delimiter", default=",", help="列の区切り文字 (既定 ,)")
        parser.add_argument(
            "--skip-rows", type=int, default=None, metavar="N",
            help="先頭 N 行を無視する (既定は 1 行目をヘッダ自動判定)",
        )
        parser.add_argument("--limit", type=int, default=None, help="取り込む最大件数")
        parser.add_argument("--since", default=None, help="この日時以上だけ取り込む (YYYY-MM-DD [HH:MM[:SS]])")
        parser.add_argument("--until", default=None, help="この日時未満だけ取り込む")
        parser.add_argument("--clear", action="store_true", help="取り込み前に既存レコードを全削除する")
        parser.add_argument("--skip-existing", action="store_true", help="既存の dt は上書きしない")
        parser.add_argument("--dry-run", action="store_true", help="DB に書き込まずに対象件数だけを報告する")
        parser.add_argument("-y", "--yes", action="store_true", help="--clear の確認プロンプトを省略する")

    def handle(self, *args, **opts):
        path = self._resolve_path(opts["path"])
        since = self._parse_boundary(opts["since"], "--since")
        until = self._parse_boundary(opts["until"], "--until")
        if since and until and until <= since:
            raise CommandError("--until は --since より後にしてください")

        rows, stats = self._read(path, opts, since, until)

        self.stdout.write(f"{path}")
        self.stdout.write(
            f"  読込 {stats['rows']:,} 行 / 対象 {len(rows):,} 件 "
            f"(読み飛ばし: 不正 {stats['bad']:,}・期間外 {stats['range']:,}・空行 {stats['blank']:,})"
        )
        for problem in stats["problems"][:5]:
            self.stdout.write(self.style.WARNING(f"    {problem}"))
        if stats["bad"] > 5:
            self.stdout.write(self.style.WARNING(f"    ほか {stats['bad'] - 5:,} 行"))

        if not rows:
            self.stdout.write(self.style.WARNING("取り込める行がありませんでした"))
            return

        first, last = min(rows), max(rows)
        self.stdout.write(f"  期間 {first:%Y-%m-%d %H:%M:%S} 〜 {last:%Y-%m-%d %H:%M:%S}")

        if opts["dry_run"]:
            existing = self._existing_dts(rows.keys())
            self.stdout.write(
                self.style.SUCCESS(
                    f"--dry-run のため書き込みませんでした "
                    f"(新規 {len(rows) - len(existing):,} 件・更新 {len(existing):,} 件)"
                )
            )
            return

        if opts["clear"]:
            if not self._confirm(Temp.objects.count(), opts):
                self.stdout.write("中止しました")
                return
            deleted, _ = Temp.objects.all().delete()
            self.stdout.write(f"  既存レコードを {deleted:,} 件削除しました")
            existing = set()
        else:
            existing = self._existing_dts(rows.keys())

        created, updated = self._write(rows, existing, skip_existing=opts["skip_existing"])
        self.stdout.write(
            self.style.SUCCESS(
                f"インポート完了: 新規 {created:,} 件 / 更新 {updated:,} 件 (全 {Temp.objects.count():,} 件)"
            )
        )

    # --- CSV を読む -----------------------------------------------------

    def _read(self, path, opts, since, until):
        """{dt: temp} を返す。同じ dt は後の行が優先。"""
        try:
            handle = path.open("r", encoding=opts["encoding"], newline="")
        except LookupError:
            raise CommandError(f"使えないエンコーディング指定です: {opts['encoding']}")
        except OSError as exc:
            raise CommandError(f"CSV が読めませんでした: {exc}")

        limit = opts["limit"]
        skip_rows = opts["skip_rows"]
        rows: dict[datetime, Decimal] = {}
        stats = {"rows": 0, "blank": 0, "bad": 0, "range": 0, "problems": []}

        with handle:
            reader = csv.reader(handle, delimiter=opts["delimiter"] or ",")
            for lineno, row in enumerate(reader, start=1):
                if lineno > MAX_ROWS:
                    raise CommandError(f"{MAX_ROWS:,} 行を超えました。ファイル指定を確認してください")
                if not any(str(cell).strip() for cell in row):
                    stats["blank"] += 1
                    continue
                if skip_rows is not None:
                    if lineno <= skip_rows:
                        continue  # 明示指定なので先頭 N 行を捨てる(中身がデータでも)
                elif stats["rows"] == 0 and self._looks_like_header(row):
                    self.stdout.write(f"  1 行目をヘッダとして無視しました: {','.join(row)[:60]}")
                    continue

                stats["rows"] += 1
                dt, temp, problem = self._parse_row(lineno, row)
                if problem:
                    stats["bad"] += 1
                    if len(stats["problems"]) < 20:
                        stats["problems"].append(problem)
                    continue
                if (since and dt < since) or (until and dt >= until):
                    stats["range"] += 1
                    continue

                rows[dt] = temp
                if limit and len(rows) >= limit:
                    break

        return rows, stats

    def _parse_row(self, lineno, row):
        """(dt, temp, 失敗時の説明) を返す。1 列目と 2 列目だけを使う。"""
        if len(row) < 2:
            return None, None, f"{lineno}行目: 列が 2 列未満 ({','.join(row)!r})"
        try:
            return _parse_dt(row[0]), _parse_temp(row[1]), None
        except (ValueError, TypeError) as exc:
            return None, None, f"{lineno}行目: {exc}"

    def _looks_like_header(self, row):
        """1 行目が日時でも温度でも読めないときだけ、ヘッダとみなす。

        どちらか一方でも読めるなら「壊れたデータ行」として不正扱いにしたいので、
        日時・温度の両方が読めないことを条件にする。
        """
        if len(row) < 2:
            return True
        try:
            _parse_dt(row[0])
            return False  # 日時として読めるならデータ行
        except (ValueError, TypeError):
            pass
        try:
            _parse_temp(row[1])
        except (ValueError, TypeError):
            return True  # 日時も温度も読めない = ヘッダ
        return False

    # --- DB に書く -----------------------------------------------------

    def _existing_dts(self, dts: Iterable[datetime]) -> set[datetime]:
        """渡した日時のうち、すでに DB へ入っているものを返す。"""
        dts = list(dts)
        found: set[datetime] = set()
        for i in range(0, len(dts), BATCH):
            found |= set(Temp.objects.filter(dt__in=dts[i : i + BATCH]).values_list("dt", flat=True))
        return found

    @transaction.atomic
    def _write(self, rows, existing, *, skip_existing):
        """rows のうち existing に無いものを作る。既存分は temp だけ更新(またはスキップ)。"""
        inserts = [(dt, temp) for dt, temp in rows.items() if dt not in existing]
        updates = [] if skip_existing else [(dt, temp) for dt, temp in rows.items() if dt in existing]

        for i in range(0, len(inserts), BATCH):
            chunk = inserts[i : i + BATCH]
            Temp.objects.bulk_create([Temp(dt=dt, temp=temp) for dt, temp in chunk])

        for i in range(0, len(updates), BATCH):
            chunk = updates[i : i + BATCH]
            Temp.objects.bulk_create(
                [Temp(dt=dt, temp=temp) for dt, temp in chunk],
                update_conflicts=True,
                update_fields=["temp"],
                unique_fields=["dt"],
            )

        return len(inserts), len(updates)

    # --- いろいろな小物 -------------------------------------------------

    def _resolve_path(self, value):
        """CSV の実在するパスを決める。相対名はカWD → プロジェクト直下 → リポジトリ直下 で探す。"""
        if value:
            target = Path(value).expanduser()
            if target.is_absolute():
                bases = [target.parent]
            else:
                bases = [Path.cwd(), Path(settings.BASE_DIR), Path(settings.BASE_DIR).parent]
            for base in bases:
                candidate = base / target
                if candidate.is_file():
                    return candidate.resolve()
            raise CommandError(f"CSV が見つかりません: {value}")

        repo_root = Path(settings.BASE_DIR).parent
        for base in (repo_root, Path.cwd(), Path(settings.BASE_DIR)):
            candidate = base / DEFAULT_CSV
            if candidate.is_file():
                return candidate.resolve()
        raise CommandError(f"CSV が見つかりません ({repo_root / DEFAULT_CSV})。パスを明示してください")

    def _parse_boundary(self, value, name):
        if value is None or str(value).strip() == "":
            return None
        try:
            return _parse_dt(value)
        except ValueError as exc:
            raise CommandError(f"{name}: {exc}")

    def _confirm(self, count, opts):
        if opts["yes"]:
            return True
        if sys.stdin is None or not sys.stdin.isatty():
            raise CommandError(f"全 {count:,} 件を削除します。よければ --yes を付けて再実行してください")
        self.stdout.write(f"既存レコード {count:,} 件を削除して取り込みます。よろしいですか? [y/N]: ", ending="")
        self.stdout.flush()
        return sys.stdin.readline().strip().lower() in {"y", "yes", "はい"}
