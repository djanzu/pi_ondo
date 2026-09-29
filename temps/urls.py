from django.urls import path

from . import views

app_name = "temps"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/temps", views.create_temp, name="create_temp"),
    path("api/temps/", views.list_temps, name="list_temps"),
    path("api/dashboard", views.dashboard_api, name="dashboard_api"),
    path("api/health", views.health, name="health"),
]
