from django.contrib import admin

from .models import Temp


@admin.register(Temp)
class TempAdmin(admin.ModelAdmin):
    list_display = ("dt", "temp")
    ordering = ("-dt",)
