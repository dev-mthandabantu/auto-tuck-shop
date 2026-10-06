"""
URL configuration for auto-tuck-shop project.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from apps.core.views import PartnersView, pilot_metrics

admin.site.site_header = "Auto Tuckshop Admin"
admin.site.site_title = "Auto Tuckshop Admin"
admin.site.index_title = "Dashboard"

urlpatterns = [
    path("", TemplateView.as_view(template_name="index.html"), name="home"),
    path("partners/", PartnersView.as_view(), name="partners"),
    path("privacy/", TemplateView.as_view(template_name="public/privacy.html"), name="privacy"),
    path("terms/", TemplateView.as_view(template_name="public/terms.html"), name="terms"),
    path(
        "data-deletion/",
        TemplateView.as_view(template_name="public/data_deletion.html"),
        name="data_deletion",
    ),
    path("admin/pilot-metrics/", pilot_metrics, name="pilot_metrics"),
    path("admin/", admin.site.urls),
    path("webhook/", include("apps.whatsapp.urls")),
]

if getattr(settings, "ENABLE_TEST_API", False):
    urlpatterns += [
        path("test/", include("apps.testing.urls")),
        path("mock-whatsapp/", include("apps.mock_whatsapp.urls")),
    ]
