import importlib.util
import json
import os
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import HttpCase, TransactionCase, tagged

REQUEST_PATH = "odoo.addons.lcc_cyclos_base.models.res_alt_currency.requests.request"
CYCLOS_WEB_URL = "https://cyclos.example.test"


def _registration_response(cyclos_id="1234", login="868256"):
    """JSON answer of Cyclos to ``POST /users``, with ``login`` as username."""
    principals = (
        [{"type": {"internalName": "username"}, "value": login}] if login else []
    )
    return json.dumps(
        {"user": {"id": cyclos_id}, "status": "active", "principals": principals}
    )


class FakeCyclos:
    """Stand-in for ``requests.request`` answering like a Cyclos server.

    ``password_status`` is the status of every user's password, or a dict
    of statuses by Cyclos user id, where ``None`` makes Cyclos unreachable
    for that user.
    """

    def __init__(self, password_status="reset", session_token="session"):
        self.password_status = password_status
        self.session_token = session_token
        self.calls = []

    def calls_to(self, path):
        return [kw for url, kw in self.calls if url.endswith(path)]

    def __call__(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/auth/session"):
            return MagicMock(text=json.dumps({"sessionToken": self.session_token}))
        if "/passwords/" in url:
            user_id = url.split("/passwords/")[0].rsplit("/", 1)[1]
            status = (
                self.password_status.get(user_id)
                if isinstance(self.password_status, dict)
                else self.password_status
            )
            if status is None:
                raise requests.exceptions.ConnectionError(url)
            return MagicMock(text=json.dumps({"status": status}))
        return MagicMock(text="{}")


class TestCyclosRestCall(TransactionCase):
    def _create_cyclos_currency(self, **values):
        unit_product = self.env.ref(
            "lcc_lokavaluto_app_connection.product_product_numeric_lcc"
        ).sudo()
        return self.env["res.alt.currency"].create(
            {
                "name": "Currency A",
                "ident": "currencyA",
                "active": True,
                "engine": "cyclos",
                "currency_unit_product_id": unit_product.id,
                "cyclos_server_url": "https://cyclos.example.test/api",
                "cyclos_server_login": "611424",
                "cyclos_server_password": "secret",
                "cyclos_web_url": CYCLOS_WEB_URL,
                **values,
            }
        )

    def _create_cyclos_wallet(self, currency, is_company=False):
        partner = self.env["res.partner"].create(
            {"name": "Holder", "is_company": is_company}
        )
        return self.env["res.partner.backend"].create(
            {
                "name": "cyclos:4036691651388677992",
                "active": True,
                "alt_currency_id": currency.id,
                "partner_id": partner.id,
                "cyclos_id": "4036691651388677992",
                "cyclos_create_response": "OK",
                "cyclos_status": "active",
            }
        )

    def _sent_requests(self, action, response_text="{}"):
        """Run ``action`` with Cyclos mocked; return the kwargs of each request."""
        response = MagicMock(text=response_text)
        with patch(REQUEST_PATH, return_value=response) as request:
            action()
        return [call.args + (call.kwargs,) for call in request.call_args_list]

    def _sent_headers(self, currency):
        """Run one ``cyclos_rest_call`` and return the HTTP headers it sent."""
        (sent,) = self._sent_requests(lambda: currency.cyclos_rest_call("GET", "/auth"))
        return sent[-1]["headers"]

    def _credit_payment_type(self, wallet):
        (sent,) = self._sent_requests(lambda: wallet.credit_wallet(10))
        return json.loads(sent[-1]["data"])["type"]

    def test_principal_type_header_sent_when_configured(self):
        currency = self._create_cyclos_currency(
            cyclos_server_principal_type="username"
        )
        self.assertEqual(self._sent_headers(currency)["Principal-Type"], "username")

    def test_no_principal_type_header_when_not_configured(self):
        currency = self._create_cyclos_currency()
        self.assertNotIn("Principal-Type", self._sent_headers(currency))

    def test_credit_uses_configured_payment_types(self):
        currency = self._create_cyclos_currency(
            cyclos_credit_payment_type_user="stableAccount.toIndividual",
            cyclos_credit_payment_type_pro="stableAccount.toProfessional",
        )
        self.assertEqual(
            self._credit_payment_type(self._create_cyclos_wallet(currency)),
            "stableAccount.toIndividual",
        )
        self.assertEqual(
            self._credit_payment_type(
                self._create_cyclos_wallet(currency, is_company=True)
            ),
            "stableAccount.toProfessional",
        )

    def test_credit_payment_types_default_to_lokavaluto_cyclos_names(self):
        currency = self._create_cyclos_currency()
        self.assertEqual(
            self._credit_payment_type(self._create_cyclos_wallet(currency)),
            "debit.toUser",
        )
        self.assertEqual(
            self._credit_payment_type(
                self._create_cyclos_wallet(currency, is_company=True)
            ),
            "debit.toPro",
        )

    def test_reconversion_search_uses_configured_account_type(self):
        currency = self._create_cyclos_currency(
            cyclos_reconversion_account_type="stableAccount"
        )
        (sent,) = self._sent_requests(
            lambda: list(currency._retrieve_last_debit_transactions()),
            response_text="[]",
        )
        self.assertIn("toAccountTypes=stableAccount&", sent[1] + "&")

    def test_wallet_balance_is_read_from_cyclos_accounts(self):
        wallet = self._create_cyclos_wallet(self._create_cyclos_currency())
        response = MagicMock(text='[{"status": {"balance": "393.57"}}]')
        with patch(REQUEST_PATH, return_value=response) as request:
            result = wallet.get_wallet_balance()
        self.assertEqual(result, {"success": True, "response": 393.57})
        self.assertTrue(
            request.call_args.args[1].endswith("/4036691651388677992/accounts"),
            request.call_args.args[1],
        )

    def test_password_change_uses_configured_password_type(self):
        currency = self._create_cyclos_currency(cyclos_password_type="login")
        wallet = self._create_cyclos_wallet(currency)
        (sent,) = self._sent_requests(lambda: wallet.force_cyclos_password("pw"))
        self.assertTrue(
            sent[1].endswith("/4036691651388677992/passwords/login/change"), sent[1]
        )

    def test_wallet_ident_set_at_creation(self):
        wallet = self._create_cyclos_wallet(self._create_cyclos_currency())
        self.assertEqual(wallet.ident, "4036691651388677992")
        self.assertTrue(wallet.uri.endswith("/wallet/4036691651388677992"), wallet.uri)

    def test_reconversion_yields_uri_of_sender_wallet(self):
        currency = self._create_cyclos_currency()
        wallet = self._create_cyclos_wallet(currency)
        transactions = [
            {
                "id": "4036655354888494952",
                "date": "2026-10-27T10:00:00.000+01:00",
                "amount": "1.00",
                "from": {"user": {"id": "4036691651388677992"}},
            }
        ]
        response = MagicMock(text=json.dumps(transactions))
        with patch(REQUEST_PATH, return_value=response), patch.object(
            self.env.cr, "commit"
        ):
            (transaction,) = list(currency._retrieve_last_debit_transactions())
        self.assertEqual(transaction["wallet_uri"], wallet.uri)
        self.assertNotIn("sender", transaction)

    def _new_holder(self, is_company=False, email="holder@example.test"):
        return self.env["res.partner"].create(
            {"name": "New holder", "is_company": is_company, "email": email}
        )

    def _registration_payload(self, currency, partner, response_text=None):
        """Register ``partner`` on ``currency``; return the JSON sent to Cyclos."""
        (sent,) = self._sent_requests(
            lambda: partner.cyclos_add_user(currency),
            response_text=response_text or _registration_response(),
        )
        return json.loads(sent[-1]["data"])

    def _registration_group(self, currency, is_company=False):
        partner = self._new_holder(is_company=is_company)
        return self._registration_payload(currency, partner)["group"]

    def _password_setup_mails(self, partner):
        return self.env["mail.mail"].search(
            [
                ("model", "=", "res.partner.backend"),
                ("res_id", "in", partner.lcc_backend_ids.ids),
                ("recipient_ids", "in", partner.ids),
            ]
        )

    def _registered_wallet(self, currency, login="868256", cyclos_id="1234"):
        """Register a new holder on ``currency``; return the wallet and the
        password Odoo sent to Cyclos."""
        partner = self._new_holder(email=f"holder-{cyclos_id}@example.test")
        payload = self._registration_payload(
            currency,
            partner,
            _registration_response(cyclos_id=cyclos_id, login=login),
        )
        return partner.lcc_backend_ids, payload["passwords"][0]["value"]

    def test_registration_is_silent_with_a_password_to_change(self):
        currency = self._create_cyclos_currency(cyclos_password_type="login")
        payload = self._registration_payload(currency, self._new_holder())
        self.assertIs(payload["skipActivationEmail"], True)
        (password,) = payload["passwords"]
        self.assertEqual(password["type"], "login")
        self.assertRegex(password["value"], r"^[A-Za-z0-9]{32}$")
        self.assertIs(password["forceChange"], True)

    def test_registration_passwords_are_random(self):
        currency = self._create_cyclos_currency()
        first, second = (
            self._registration_payload(
                currency, self._new_holder(email=f"holder{i}@example.test")
            )["passwords"][0]["value"]
            for i in range(2)
        )
        self.assertNotEqual(first, second)

    def test_temporary_password_kept_until_chosen(self):
        wallet, password = self._registered_wallet(self._create_cyclos_currency())
        self.assertEqual(wallet.sudo().cyclos_initial_password, password)
        self.assertTrue(wallet.cyclos_password_pending)

    def test_temporary_password_readable_by_administrators_only(self):
        wallet, _password = self._registered_wallet(self._create_cyclos_currency())
        user = self.env["res.users"].create(
            {
                "name": "Wallet reader",
                "login": "wallet-reader",
                "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )
        for field in ("cyclos_initial_password", "cyclos_password_setup_token"):
            with self.assertRaises(AccessError):
                wallet.with_user(user).read([field])

    def test_temporary_password_not_in_history_nor_email(self):
        currency = self._create_cyclos_currency()
        wallet, password = self._registered_wallet(currency)
        kept = [
            json.dumps(wallet.message_ids.read(["body"]), default=str),
            json.dumps(wallet.message_ids.tracking_value_ids.read(), default=str),
            *self._password_setup_mails(wallet.partner_id).mapped("body_html"),
        ]
        self.assertNotIn(password, "".join(kept))

    def test_registration_stores_cyclos_login(self):
        wallet, _password = self._registered_wallet(
            self._create_cyclos_currency(), login="868256"
        )
        self.assertEqual(wallet.cyclos_login, "868256")

    def test_registration_sends_password_setup_link(self):
        wallet, _password = self._registered_wallet(
            self._create_cyclos_currency(), login="868256"
        )
        (mail,) = self._password_setup_mails(wallet.partner_id)
        token = wallet.sudo().cyclos_password_setup_token
        self.assertTrue(token)
        self.assertIn(
            f"{wallet.get_base_url()}/cyclos/set-password/{token}", mail.body_html
        )
        self.assertIn("868256", mail.body_html)

    def test_registration_refused_without_cyclos_web_url(self):
        currency = self._create_cyclos_currency(cyclos_web_url=False)
        partner = self._new_holder()
        with patch(REQUEST_PATH) as request:
            with self.assertRaises(UserError):
                partner.cyclos_add_user(currency)
        request.assert_not_called()
        self.assertFalse(partner.lcc_backend_ids)

    def test_registration_fails_without_cyclos_login(self):
        currency = self._create_cyclos_currency()
        partner = self._new_holder()
        with self.assertRaisesRegex(UserError, "4036690082383437672"):
            self._registration_payload(
                currency,
                partner,
                _registration_response(cyclos_id="4036690082383437672", login=None),
            )

    def test_resending_email_replaces_link(self):
        wallet, _password = self._registered_wallet(self._create_cyclos_currency())
        first = wallet.sudo().cyclos_password_setup_token
        wallet.cyclos_send_password_setup_email()
        second = wallet.sudo().cyclos_password_setup_token
        self.assertEqual(len(self._password_setup_mails(wallet.partner_id)), 2)
        self.assertNotEqual(first, second)
        Wallet = self.env["res.partner.backend"]
        self.assertFalse(Wallet._cyclos_wallet_by_setup_token(first))
        self.assertEqual(Wallet._cyclos_wallet_by_setup_token(second), wallet)

    def test_link_opens_member_session_while_password_to_change(self):
        currency = self._create_cyclos_currency(
            cyclos_web_url="https://cyclos.example.test/",
            cyclos_password_type="login",
        )
        wallet, password = self._registered_wallet(currency, login="868256")
        cyclos = FakeCyclos(password_status="reset", session_token="member-session")
        with patch(REQUEST_PATH, side_effect=cyclos):
            url = wallet.cyclos_password_setup_redirect()
        self.assertEqual(url, "https://cyclos.example.test/?sessionToken=member-session")
        (session,) = cyclos.calls_to("/auth/session")
        self.assertEqual(session["auth"].username, "868256")
        self.assertEqual(session["auth"].password, password)
        self.assertEqual(session["headers"]["Channel"], "main")
        self.assertEqual(session["headers"]["Principal-Type"], "username")
        self.assertEqual(wallet.sudo().cyclos_initial_password, password)

    def test_chosen_password_is_forgotten(self):
        currency = self._create_cyclos_currency(
            cyclos_web_url="https://cyclos.example.test"
        )
        wallet, _password = self._registered_wallet(currency)
        token = wallet.sudo().cyclos_password_setup_token
        cyclos = FakeCyclos(password_status="active")
        with patch(REQUEST_PATH, side_effect=cyclos):
            url = wallet.cyclos_password_setup_redirect()
        self.assertEqual(url, "https://cyclos.example.test/")
        self.assertFalse(cyclos.calls_to("/auth/session"))
        self.assertFalse(wallet.sudo().cyclos_initial_password)
        self.assertFalse(wallet.cyclos_password_pending)
        ## The link keeps leading to Cyclos, now to its login page.
        again = self.env["res.partner.backend"]._cyclos_wallet_by_setup_token(token)
        with patch(REQUEST_PATH) as request:
            self.assertEqual(
                again.cyclos_password_setup_redirect(), "https://cyclos.example.test/"
            )
        request.assert_not_called()

    def test_link_returns_to_configured_cyclos_page_after_password(self):
        currency = self._create_cyclos_currency(
            cyclos_web_url="https://cyclos.example.test",
            cyclos_password_setup_return_page="odooRedirectPage",
        )
        wallet, _password = self._registered_wallet(currency)
        cyclos = FakeCyclos(password_status="reset", session_token="member-session")
        with patch(REQUEST_PATH, side_effect=cyclos):
            url = urlsplit(wallet.cyclos_password_setup_redirect())
        self.assertEqual(
            (url.scheme, url.netloc, url.path), ("https", "cyclos.example.test", "/")
        )
        self.assertEqual(
            parse_qs(url.query),
            {
                "sessionToken": ["member-session"],
                "returnTo": ["page-content!page=odooRedirectPage"],
            },
        )

    def test_no_return_page_once_password_chosen(self):
        currency = self._create_cyclos_currency(
            cyclos_web_url="https://cyclos.example.test",
            cyclos_password_setup_return_page="odooRedirectPage",
        )
        wallet, _password = self._registered_wallet(currency)
        with patch(REQUEST_PATH, side_effect=FakeCyclos(password_status="active")):
            url = wallet.cyclos_password_setup_redirect()
        self.assertEqual(url, "https://cyclos.example.test/")

    def test_password_kept_when_cyclos_status_unknown(self):
        wallet, password = self._registered_wallet(self._create_cyclos_currency())
        with patch(REQUEST_PATH, side_effect=requests.exceptions.ConnectionError):
            with self.assertRaises(requests.exceptions.ConnectionError):
                wallet.cyclos_password_setup_redirect()
        self.assertEqual(wallet.sudo().cyclos_initial_password, password)
        self.assertTrue(wallet.cyclos_password_pending)

    def test_cron_forgets_only_chosen_passwords(self):
        currency = self._create_cyclos_currency()
        chosen, _p1 = self._registered_wallet(currency, cyclos_id="1")
        pending, p2 = self._registered_wallet(currency, cyclos_id="2")
        unreachable, p3 = self._registered_wallet(currency, cyclos_id="3")
        cyclos = FakeCyclos(
            password_status={
                chosen.cyclos_id: "active",
                pending.cyclos_id: "reset",
                unreachable.cyclos_id: None,
            }
        )
        with patch(REQUEST_PATH, side_effect=cyclos):
            self.env["res.partner.backend"]._cron_cyclos_check_password_setup()
        self.assertFalse(chosen.sudo().cyclos_initial_password)
        self.assertEqual(pending.sudo().cyclos_initial_password, p2)
        self.assertEqual(unreachable.sudo().cyclos_initial_password, p3)

    def test_registration_refused_without_email(self):
        currency = self._create_cyclos_currency()
        partner = self._new_holder(email=False)
        with patch(REQUEST_PATH) as request:
            with self.assertRaises(UserError):
                partner.cyclos_add_user(currency)
        request.assert_not_called()
        self.assertFalse(partner.lcc_backend_ids)

    def test_registration_uses_configured_groups(self):
        currency = self._create_cyclos_currency(
            cyclos_user_group_person="individual",
            cyclos_user_group_company="professional",
        )
        self.assertEqual(self._registration_group(currency), "individual")
        self.assertEqual(
            self._registration_group(currency, is_company=True), "professional"
        )

    def test_registration_groups_default_to_lokavaluto_cyclos_names(self):
        currency = self._create_cyclos_currency()
        self.assertEqual(self._registration_group(currency), "particuliers")
        self.assertEqual(
            self._registration_group(currency, is_company=True), "professionnels"
        )

@tagged("post_install", "-at_install")
class TestCyclosPasswordSetupRoute(HttpCase):
    """The link of the password setup e-mail, opened in a browser."""

    def setUp(self):
        super().setUp()
        ## A visitor's session, bound to the test database.
        self.authenticate(None, None)

    def _pending_wallet(self, token):
        currency = self.env["res.alt.currency"].create(
            {
                "name": "Currency B",
                "ident": "currencyB",
                "active": True,
                "engine": "cyclos",
                "currency_unit_product_id": self.env.ref(
                    "lcc_lokavaluto_app_connection.product_product_numeric_lcc"
                ).id,
                "cyclos_server_url": "https://cyclos.example.test/api",
                "cyclos_server_login": "611424",
                "cyclos_server_password": "secret",
                "cyclos_web_url": CYCLOS_WEB_URL,
            }
        )
        partner = self.env["res.partner"].create({"name": "Holder"})
        return self.env["res.partner.backend"].create(
            {
                "name": "cyclos:4036691651388677992",
                "alt_currency_id": currency.id,
                "partner_id": partner.id,
                "cyclos_id": "4036691651388677992",
                "type": "cyclos",
                "cyclos_login": "868256",
                "cyclos_status": "active",
                "cyclos_initial_password": "temporary",
                "cyclos_password_setup_token": token,
                "cyclos_password_pending": True,
            }
        )

    def test_link_redirects_to_cyclos_with_member_session(self):
        self._pending_wallet("a-valid-token")
        cyclos = FakeCyclos(password_status="reset", session_token="member-session")
        with patch(REQUEST_PATH, side_effect=cyclos):
            response = self.url_open(
                "/cyclos/set-password/a-valid-token", allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["Location"],
            "https://cyclos.example.test/?sessionToken=member-session",
        )
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_unknown_link_is_not_found(self):
        self._pending_wallet("a-valid-token")
        with patch(REQUEST_PATH) as request:
            response = self.url_open(
                "/cyclos/set-password/another-token", allow_redirects=False
            )
        self.assertEqual(response.status_code, 404)
        request.assert_not_called()


class TestCyclosWalletIdentMigration(TransactionCase):
    """Wallets created before ``ident`` was set at creation get it back."""

    def _migrate(self):
        path = get_module_path("lcc_cyclos_base")
        spec = importlib.util.spec_from_file_location(
            "lcc_cyclos_base_post_migrate_16_0_1_2_1",
            os.path.join(path, "migrations", "16.0.1.2.1", "post-migrate.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "16.0.1.2.0")
        self.env.invalidate_all()

    def _wallet(self, currency, cyclos_id, ident):
        partner = self.env["res.partner"].create({"name": f"Holder {cyclos_id}"})
        wallet = self.env["res.partner.backend"].create(
            {
                "name": f"cyclos:{cyclos_id}",
                "alt_currency_id": currency.id,
                "partner_id": partner.id,
                "cyclos_id": cyclos_id,
            }
        )
        ## Simulate wallets stored before the fix, bypassing the ORM.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE res_partner_backend SET ident = %s, uri = %s WHERE id = %s",
            (ident, f"{currency.uri}/wallet/{ident}", wallet.id),
        )
        self.env.invalidate_all(flush=False)
        return wallet

    def test_missing_wallet_ident_filled_from_cyclos_id(self):
        currency = self.env["res.alt.currency"].create(
            {"name": "C", "ident": "cyclos.example.test", "engine": "cyclos"}
        )
        missing = self._wallet(currency, "111", None)
        kept = self._wallet(currency, "222", "already-set")

        self._migrate()

        self.assertEqual(missing.ident, "111")
        self.assertEqual(missing.uri, f"{currency.uri}/wallet/111")
        self.assertEqual(kept.ident, "already-set")


class TestCyclosWebUrlMigration(TransactionCase):
    """Currencies set up with the forgotten password page keep their host."""

    def _migrate(self):
        path = get_module_path("lcc_cyclos_base")
        spec = importlib.util.spec_from_file_location(
            "lcc_cyclos_base_pre_migrate_16_0_1_4_0",
            os.path.join(path, "migrations", "16.0.1.4.0", "pre-migrate.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "16.0.1.3.0")

    def test_forgotten_password_page_becomes_web_address(self):
        currency = self.env.ref("lcc_cyclos_base.res_alt_currency_cyclos_1")
        self.env.flush_all()
        cr = self.env.cr
        ## Simulate the column of version 16.0.1.3.0, bypassing the ORM.
        cr.execute(
            "ALTER TABLE res_alt_currency "
            "RENAME COLUMN cyclos_web_url TO cyclos_password_setup_url"
        )
        cr.execute(
            "UPDATE res_alt_currency SET cyclos_password_setup_url = %s WHERE id = %s",
            ("https://cyclos.example.test/#access.login.forgot-password", currency.id),
        )
        self._migrate()
        cr.execute(
            "SELECT cyclos_web_url FROM res_alt_currency WHERE id = %s", (currency.id,)
        )
        self.assertEqual(cr.fetchone()[0], "https://cyclos.example.test")

    def test_old_password_setup_template_removed_for_reload(self):
        self._migrate()
        self.env.invalidate_all()
        self.assertFalse(
            self.env.ref(
                "lcc_cyclos_base.mail_template_cyclos_password_setup",
                raise_if_not_found=False,
            )
        )
