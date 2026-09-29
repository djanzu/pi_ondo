import io
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.utils import timezone

from temps.models import Temp
from temps.services import build_dashboard, replace_years


def post_temp(dt_str, temp):
    """記録用 API に 1 件 POST するヘルパー。"""
    return Client().post(
        "/api/temps",
        data=json.dumps({"dt": dt_str, "temp": temp}),
        content_type="application/json",
    )


class CreateApiTest(TestCase):
    def test_create_ok(self):
        res = post_temp("2026-09-29 14:16:00", "29.1")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["temp"], 29.1)
        row = Temp.objects.get()
        self.assertEqual(str(row.temp), "29.1")
        self.assertEqual(timezone.localtime(row.dt).strftime("%Y-%m-%d %H:%M:%S"), "2026-09-29 14:16:00")

    def test_create_decimal_1_digit(self):
        res = post_temp("2026-09-29 14:16:00", "29.06")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(str(Temp.objects.get().temp), "29.1")

    def test_create_upserts_same_dt(self):
        post_temp("2026-09-29 14:16:00", "29.1")
        res = post_temp("2026-09-29 14:16:00", "-3.4")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["created"])
        self.assertEqual(Temp.objects.count(), 1)
        self.assertEqual(str(Temp.objects.get().temp), "-3.4")

    def test_create_accepts_several_formats(self):
        """書式の違う指定はすべて同じ 1 件として扱える。"""
        for value in [
            "2026-09-29 08:05",
            "2026-09-29T08:05:00",
            "2026-09-29 08:05:00",
            "2026-09-29T08:05:00.500",
            "2026/09/29 08:05:00",
        ]:
            res = post_temp(value, "20.0")
            self.assertIn(res.status_code, (200, 201), value)
        self.assertEqual(Temp.objects.count(), 1)
        self.assertEqual(timezone.localtime(Temp.objects.get().dt).strftime("%H:%M"), "08:05")

    def test_create_date_only(self):
        """日付だけの指定は 00:00 として扱う。"""
        self.assertEqual(post_temp("2026-09-29", "20.0").status_code, 201)
        self.assertEqual(timezone.localtime(Temp.objects.get().dt).strftime("%H:%M"), "00:00")

    def test_create_defaults_dt_when_empty(self):
        res = post_temp("", "12.3")
        self.assertEqual(res.status_code, 201)
        row = Temp.objects.get()
        self.assertLess(abs(row.dt - timezone.now()), timedelta(minutes=1))

    def test_create_rejects_bad_payload(self):
        self.assertEqual(post_temp("not-a-date", "20.0").status_code, 400)
        self.assertEqual(post_temp("2026-09-29 14:16:00", "abc").status_code, 400)
        self.assertEqual(post_temp("2026-09-29 14:16:00", "1000").status_code, 400)
        self.assertEqual(post_temp("2026-09-29 14:16:00", "").status_code, 400)
        self.assertEqual(Temp.objects.count(), 0)

    def test_create_rejects_non_object_json(self):
        from django.test import Client

        res = Client().post("/api/temps", data="[]", content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_create_rejects_get(self):
        self.assertEqual(self.client.get("/api/temps").status_code, 405)


class ReadApiTest(TestCase):
    def setUp(self):
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        for i in range(20):
            Temp.objects.create(dt=now - timedelta(minutes=i * 10), temp=20 + i / 10)

    def test_default_count(self):
        body = self.client.get("/api/temps/").json()
        self.assertEqual(body["count"], 20)
        self.assertEqual(body["requested"], 72)
        self.assertEqual(len(body["results"]), 20)

    def test_count_limits(self):
        body = self.client.get("/api/temps/?count=5").json()
        self.assertEqual(body["count"], 5)
        # 新しい順
        self.assertGreater(body["results"][0]["dt"], body["results"][-1]["dt"])

    def test_order_asc(self):
        body = self.client.get("/api/temps/?count=3&order=asc").json()
        self.assertLess(body["results"][0]["dt"], body["results"][-1]["dt"])

    def test_hours_window(self):
        old = timezone.localtime(timezone.now()) - timedelta(days=2)
        Temp.objects.create(dt=old, temp=1.0)
        body = self.client.get("/api/temps/?hours=12&count=100").json()
        self.assertEqual(body["count"], 20)

    def test_range_params(self):
        """start 以上 end 未満で絞る(end は含めない)。"""
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        url = "/api/temps/?count=100&start=%s&end=%s" % (
            (now - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
            (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        body = self.client.get(url).json()
        self.assertEqual([r["temp"] for r in body["results"]], [20.0, 20.1, 20.2])

        body = self.client.get(
            "/api/temps/?count=100&start=%s&end=%s"
            % (
                (now - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d %H:%M:%S"),
            )
        ).json()
        self.assertEqual(body["count"], 2)

    def test_year_ago(self):
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        Temp.objects.create(dt=replace_years(now - timedelta(minutes=10), -1), temp=11.1)
        body = self.client.get("/api/temps/?count=100&year_ago=1&hours=12").json()
        self.assertEqual(body["count"], 1)
        self.assertIn(str(now.year - 1), body["results"][0]["dt"])

    def test_bad_params(self):
        self.assertEqual(self.client.get("/api/temps/?count=0").status_code, 400)
        self.assertEqual(self.client.get("/api/temps/?count=abc").status_code, 400)
        self.assertEqual(self.client.get("/api/temps/?start=xyz").status_code, 400)


class DashboardTest(TestCase):
    def setUp(self):
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        for i in range(72):
            Temp.objects.create(dt=now - timedelta(minutes=i * 10), temp=18 + (i % 12))
        # 1年前: 同じ月日時分
        for i in range(72):
            Temp.objects.create(dt=replace_years(now - timedelta(minutes=i * 10), -1), temp=8 + (i % 9))

    def test_dashboard_renders(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "温度記録")
        self.assertContains(res, "現在")
        self.assertContains(res, "1年前")
        self.assertContains(res, "折れ線")  # aria-label に含む
        self.assertContains(res, "is-max")
        self.assertContains(res, "is-year-min")

    def test_dashboard_shows_12_hours(self):
        data = build_dashboard(hours=12, count=72)
        self.assertEqual(data["table_current"].count, 72)
        self.assertEqual(data["table_year_ago"].count, 72)
        self.assertIsNotNone(data["table_current"].max_row)
        self.assertEqual(
            data["table_current"].max_row["temp"], max(r["temp"] for r in data["series_current"])
        )

    def test_year_ago_matches_same_clock_time(self):
        data = build_dashboard(hours=12, count=72)
        for row in data["table_year_ago"].rows:
            self.assertEqual(row["dt"].month, row["dt"].month)
        # 1年前の各行が、現在のどこかの時刻のちょうど1年前に対応づいている
        current_times = {replace_years(r["dt"], -1) for r in data["series_current"]}
        for row in data["table_year_ago"].rows:
            self.assertIn(row["dt"], current_times)

    def test_year_ago_picks_nearest_within_tolerance(self):
        Temp.objects.all().delete()
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        Temp.objects.create(dt=now, temp=25.0)
        # 1年前は 4分 だけズレた位置にしかデータが無い
        Temp.objects.create(dt=replace_years(now, -1) + timedelta(minutes=4), temp=11.0)
        data = build_dashboard(hours=12, count=72)
        self.assertEqual(data["table_year_ago"].count, 1)
        self.assertEqual(data["table_year_ago"].rows[0]["temp"], 11.0)

    def test_year_ago_ignores_far_records(self):
        Temp.objects.all().delete()
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        Temp.objects.create(dt=now, temp=25.0)
        Temp.objects.create(dt=replace_years(now, -1) + timedelta(minutes=45), temp=11.0)
        data = build_dashboard(hours=12, count=72)
        self.assertEqual(data["table_year_ago"].count, 0)

    def test_empty_db(self):
        Temp.objects.all().delete()
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "該当する記録がありません")
        self.assertContains(res, "1年前の記録がありません")

    def test_dashboard_api(self):
        body = self.client.get("/api/dashboard?hours=12&count=72").json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["current"]), 72)
        self.assertEqual(len(body["year_ago"]), 72)
        self.assertEqual(body["hours"], 12)

    def test_health(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["records"], 144)


class ReplaceYearsTest(TestCase):
    def test_leap_day(self):
        from datetime import datetime

        self.assertEqual(replace_years(datetime(2024, 2, 29, 14, 16), -1).date().isoformat(), "2023-02-28")
        self.assertEqual(replace_years(datetime(2025, 2, 28, 0, 0), -1).date().isoformat(), "2024-02-28")


class FakeStdin(io.StringIO):
    """確認プロンプトの stdin を偽装する (対話的なターミナルに見せる)。"""

    def isatty(self):
        return True


class ClearTempsCommandTest(TestCase):
    """manage.py clear_temps (モデル temp のクリア) の挙動。"""

    def setUp(self):
        self.now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        for i in range(10):  # 現在側 10 件 (10 分間隔)
            Temp.objects.create(dt=self.now - timedelta(minutes=i * 10), temp=20 + i / 10)
        for i in range(5):  # 1 年前側 5 件
            Temp.objects.create(dt=replace_years(self.now - timedelta(minutes=i * 10), -1), temp=15 + i / 10)

    def clear(self, *args, answer=None):
        """コマンドを走らせて標準出力を返す。answer は確認プロンプトへの入力。"""
        out = io.StringIO()
        if answer is None:
            call_command("clear_temps", *args, stdout=out)
        else:
            with mock.patch("sys.stdin", FakeStdin(f"{answer}\n")):
                call_command("clear_temps", *args, stdout=out)
        return out.getvalue()

    def test_clear_all_with_yes(self):
        out = self.clear("--yes")
        self.assertEqual(Temp.objects.count(), 0)
        self.assertIn("削除対象: 15 件 / 全 15 件 (全期間)", out)
        self.assertIn("15 件削除しました", out)

    def test_clear_confirmed_interactively(self):
        self.clear(answer="y")
        self.assertEqual(Temp.objects.count(), 0)

    def test_clear_declined_keeps_records(self):
        out = self.clear(answer="n")
        self.assertEqual(Temp.objects.count(), 15)
        self.assertIn("中止しました", out)

    def test_clear_refuses_without_confirmation(self):
        """対話的でない環境 (パイプや cron) では --yes なしで消さない。"""
        with mock.patch("sys.stdin", io.StringIO("y\n")):
            with self.assertRaises(CommandError):
                self.clear()
        self.assertEqual(Temp.objects.count(), 15)

    def test_clear_days_deletes_only_old_records(self):
        self.clear("--days", "30", "--yes")
        self.assertEqual(Temp.objects.count(), 10)  # 1 年前側だけ消える
        self.assertTrue(all(r.dt > self.now - timedelta(days=1) for r in Temp.objects.all()))

    def test_clear_year_ago_only(self):
        out = self.clear("--year-ago", "--yes")
        self.assertEqual(Temp.objects.count(), 10)
        self.assertIn("1 年前", out)

    def test_clear_range(self):
        """--before / --after で期間指定 (after 以上, before 未満)。"""
        fmt = "%Y-%m-%d %H:%M:%S"
        out = self.clear(
            "--before",
            (self.now - timedelta(minutes=25)).strftime(fmt),
            "--after",
            (self.now - timedelta(minutes=45)).strftime(fmt),
            "--yes",
        )
        self.assertEqual(Temp.objects.count(), 13)  # now-30 分, now-40 分 の 2 件
        self.assertIn("2 件削除しました", out)

    def test_clear_dry_run_keeps_records(self):
        out = self.clear("--dry-run")
        self.assertEqual(Temp.objects.count(), 15)
        self.assertIn("削除対象: 15 件", out)
        self.assertIn("--dry-run のため削除しませんでした", out)

    def test_clear_backup_can_be_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dump.json"
            out = self.clear("--backup", str(path), "--yes")
            self.assertIn("に保存しました", out)
            self.assertEqual(Temp.objects.count(), 0)
            call_command("loaddata", str(path), verbosity=0)
            self.assertEqual(Temp.objects.count(), 15)
            self.assertEqual(str(Temp.objects.order_by("dt").first().temp), "15.4")

    def test_clear_when_nothing_to_delete(self):
        Temp.objects.all().delete()
        self.assertIn("削除するレコードはありません", self.clear("--yes"))

    def test_clear_rejects_bad_arguments(self):
        fmt = "%Y-%m-%d %H:%M:%S"
        same = self.now.strftime(fmt)
        cases = [
            ("--days", "0"),
            ("--before", "not-a-date"),
            ("--year-ago", "--years", "0"),
            ("--before", same, "--after", same),
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(CommandError):
                    self.clear(*args, "--yes")
        self.assertEqual(Temp.objects.count(), 15)
