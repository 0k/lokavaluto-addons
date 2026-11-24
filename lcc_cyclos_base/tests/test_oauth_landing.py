from odoo.tests.common import TransactionCase, new_test_user


class TestCyclosOAuthLanding(TransactionCase):
    """Where a member lands after logging in with "Connect with Cyclos"."""

    def _provider(self, endpoint_base):
        return self.env["auth.oauth.provider"].create(
            {
                "name": endpoint_base,
                "client_id": "odoo",
                "auth_endpoint": f"{endpoint_base}/authorize",
                "validation_endpoint": f"{endpoint_base}/token",
                "body": "Connect",
            }
        )

    def _cyclos_provider(self):
        return self._provider("https://cyclos.example.test/api/oidc")

    def _portal_user(self):
        return new_test_user(self.env, login="member", groups="base.group_portal")

    def _internal_user(self):
        return new_test_user(self.env, login="staff", groups="base.group_user")

    def test_portal_member_lands_on_portal_home(self):
        landing = self._cyclos_provider()._cyclos_landing_url(self._portal_user(), "/")
        self.assertEqual(landing, "/my/home")

    def test_internal_user_keeps_odoo_landing(self):
        landing = self._cyclos_provider()._cyclos_landing_url(
            self._internal_user(), "/"
        )
        self.assertEqual(landing, "/")

    def test_other_provider_keeps_odoo_landing(self):
        provider = self._provider("https://accounts.example.org/o/oauth2")
        landing = provider._cyclos_landing_url(self._portal_user(), "/")
        self.assertEqual(landing, "/")

    def test_explicit_target_is_kept(self):
        provider = self._cyclos_provider()
        member = self._portal_user()
        for location in ("/shop", "/my/wallets", "/web/login?oauth_error=3"):
            self.assertEqual(provider._cyclos_landing_url(member, location), location)
