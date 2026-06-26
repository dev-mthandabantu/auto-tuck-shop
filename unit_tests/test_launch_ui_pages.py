from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class PublicLaunchPagesTestCase(TestCase):
    def test_home_page_is_evergreen_and_visual(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Record tuck shop sales from WhatsApp.")
        self.assertContains(response, "WhatsApp sales tracking for tuck shops in Zimbabwe.")
        self.assertContains(response, "auto_tuck_shop/img/app-icon.png")
        self.assertContains(response, "From message to sales record.")
        self.assertNotContains(response, "pilot program")
        self.assertNotContains(response, "closed beta")
        self.assertNotContains(response, "waitlist")

    def test_policy_pages_render_publicly(self):
        expected = {
            "privacy": "Privacy Policy",
            "terms": "Terms of Service",
            "data_deletion": "Data Deletion",
        }

        for url_name, heading in expected.items():
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, heading)
                self.assertContains(response, "Auto Tuck Shop")
                self.assertContains(response, reverse("privacy"))
                self.assertContains(response, reverse("terms"))
                self.assertContains(response, reverse("data_deletion"))


class AdminMetricsAccessTestCase(TestCase):
    def test_metrics_requires_staff_login(self):
        response = self.client.get(reverse("pilot_metrics"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_staff_can_view_metrics(self):
        user = User.objects.create_user(
            username="staff",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("pilot_metrics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilot Metrics")
        self.assertContains(response, "Sales per Shop")
