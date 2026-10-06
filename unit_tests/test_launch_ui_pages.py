from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


class PublicLaunchPagesTestCase(TestCase):
    def test_home_page_is_evergreen_and_visual(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send a WhatsApp. Confirm the sale. Get the report.")
        self.assertContains(response, "Auto Tuckshop helps Zimbabwean tuck shops record sales")
        self.assertContains(response, "auto_tuck_shop/img/app-icon.png")
        self.assertContains(response, "Built for a busy counter, not a quiet office.")
        self.assertNotContains(response, "pilot program")
        self.assertNotContains(response, "closed beta")
        self.assertContains(response, "Join the waitlist on WhatsApp")
        self.assertNotContains(response, "founder-panel")
        self.assertNotContains(response, "team-grid")

    def test_partners_page_has_team_and_thesis(self):
        response = self.client.get(reverse("partners"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "For funders, partners and press")
        self.assertContains(response, "Built close to the problem.")
        self.assertContains(response, "Brighton")
        self.assertNotContains(response, "Tandabantu")
        self.assertContains(response, "Financial access")
        self.assertNotContains(response, "pilot program")
        self.assertNotContains(response, "closed beta")

    @override_settings(PARTNER_CONTACT_EMAIL="partners@example.com", PARTNER_WHATSAPP_NUMBER="")
    def test_partners_contact_has_email_and_no_whatsapp_by_default(self):
        response = self.client.get(reverse("partners"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mailto:partners@example.com")
        self.assertContains(response, 'data-whatsapp=""')
        self.assertNotContains(response, 'data-action="send-whatsapp"')

    @override_settings(PARTNER_WHATSAPP_NUMBER="263700000000")
    def test_partners_contact_shows_whatsapp_number_when_configured(self):
        response = self.client.get(reverse("partners"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-whatsapp="263700000000"')
        self.assertContains(response, 'data-action="send-whatsapp"')

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
                self.assertContains(response, "Auto Tuckshop")
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
