from django.db import models


class Temp(models.Model):
    """温度記録。dt = 記録日時、temp = 温度。"""

    dt = models.DateTimeField("記録日時", unique=True, db_index=True)
    temp = models.DecimalField("温度", max_digits=4, decimal_places=1)

    class Meta:
        db_table = "temps"
        verbose_name = "温度記録"
        verbose_name_plural = "温度記録"
        ordering = ["-dt", "-id"]

    def __str__(self):
        return f"{self.dt:%Y-%m-%d %H:%M:%S} {self.temp}"
