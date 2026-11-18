import json
from unittest.mock import MagicMock, patch

from psycopg2 import IntegrityError

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

REQUEST_PATH = "odoo.addons.lcc_cyclos_base.models.res_alt_currency.requests.request"


class FakeCyclosUsers:
    """Stand-in for ``requests.request`` answering the Cyclos user calls.

    ``edit_data`` maps a Cyclos user id to the ``user`` object that
    ``GET /users/{id}/data-for-edit`` returns; ``refuse`` lists the user
    ids whose update Cyclos refuses; ``editable`` lists the fields Cyclos
    lets Odoo set, when creating or editing a user.
    """

    def __init__(self, edit_data=None, refuse=(), editable=("name", "username", "email", "city", "other")):
        self.edit_data = edit_data or {}
        self.refuse = set(refuse)
        self.field_actions = {name: {"edit": True} for name in editable}
        self.calls = []

    def sent(self, method, path):
        return [
            json.loads(kw["data"])
            for m, url, kw in self.calls
            if m.lower() == method and url.endswith(path)
        ]

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/data-for-edit"):
            user_id = url.split("/users/")[1].split("/")[0]
            return MagicMock(
                text=json.dumps(
                    {
                        "user": self.edit_data[user_id],
                        "profileFieldActions": self.field_actions,
                    }
                )
            )
        if "/users/data-for-new" in url:
            return MagicMock(text=json.dumps({"profileFieldActions": self.field_actions}))
        if method.lower() == "put":
            user_id = url.rsplit("/", 1)[1]
            if user_id in self.refuse:
                raise ValueError("Cyclos serveur complained about:\n  - refused")
            return MagicMock(text="")
        if method.lower() == "post" and url.endswith("/users"):
            return MagicMock(
                text=json.dumps(
                    {
                        "user": {"id": "1234"},
                        "status": "active",
                        "principals": [
                            {"type": {"internalName": "username"}, "value": "868256"}
                        ],
                    }
                )
            )
        return MagicMock(text="{}")


class TestCyclosUserFields(TransactionCase):
    """Fields of Cyclos users computed in Odoo from a mapping on the currency."""

    def _currency(self, mapping=None):
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
                "cyclos_web_url": "https://cyclos.example.test",
                "cyclos_group_rule_ids": [
                    (0, 0, {"name": "Everyone", "cyclos_group": "members"})
                ],
                "cyclos_user_field_ids": [
                    (0, 0, {"name": name, "value": value})
                    for name, value in (mapping or {}).items()
                ],
            }
        )

    def _wallet(self, currency, cyclos_id="1234", **partner_values):
        partner = self.env["res.partner"].create(
            {
                "name": "Holder",
                "email": f"holder-{cyclos_id}@example.test",
                "city": "Nantes",
                **partner_values,
            }
        )
        return self.env["res.partner.backend"].create(
            {
                "name": f"cyclos:{cyclos_id}",
                "type": "cyclos",
                "alt_currency_id": currency.id,
                "partner_id": partner.id,
                "cyclos_id": cyclos_id,
                "cyclos_login": "868256",
                "cyclos_status": "active",
            }
        )

    def _values(self, mapping, **partner_values):
        currency = self._currency(mapping)
        wallet = self._wallet(currency, **partner_values)
        return currency._cyclos_user_values(wallet)

    ## Evaluation rules (those of the connector's ``MappingProxy``)

    def test_value_naming_a_wallet_field_gives_that_field(self):
        self.assertEqual(self._values({"login": "cyclos_login"}), {"login": "868256"})

    def test_value_is_otherwise_python_with_wallet_fields_as_names(self):
        self.assertEqual(
            self._values({"city": "partner_id.city", "sum": "1 + 2", "s": '"x"'}),
            {"city": "Nantes", "sum": "3", "s": "x"},
        )

    def test_equal_sign_forces_python(self):
        self.assertEqual(self._values({"login": "=cyclos_login + '!'"}), {"login": "868256!"})

    def test_record_names_the_wallet(self):
        self.assertEqual(
            self._values({"name": "record.partner_id.name"}), {"name": "Holder"}
        )

    ## Conversion of the results to the text Cyclos expects

    def test_results_are_converted_to_cyclos_text(self):
        values = self._values(
            {
                "empty": "partner_id.street",
                "yes": "True",
                "no": "False",
                "day": "partner_id.create_date.date()",
                "number": "2.5",
            }
        )
        self.assertEqual(values["empty"], "")
        self.assertEqual(values["yes"], "true")
        self.assertEqual(values["no"], "false")
        self.assertRegex(values["day"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(values["number"], "2.5")

    ## Refusals

    def test_record_result_is_refused(self):
        with self.assertRaisesRegex(UserError, "partner"):
            self._values({"partner": "partner_id"})

    def test_methods_of_records_are_refused(self):
        with self.assertRaisesRegex(UserError, "search"):
            self._values({"count": "partner_id.search([])"})

    def test_failing_expression_names_the_cyclos_field(self):
        with self.assertRaisesRegex(UserError, "broken"):
            self._values({"broken": "1 / 0"})

    def test_cyclos_field_mapped_once_per_currency(self):
        currency = self._currency({"city": "partner_id.city"})
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            currency.write(
                {"cyclos_user_field_ids": [(0, 0, {"name": "city", "value": "1"})]}
            )
            currency.flush_recordset()

    ## Fields read, as paths from the wallet

    def test_evaluation_records_paths_of_fields_read(self):
        currency = self._currency(
            {"city": "partner_id.city", "login": "cyclos_login", "n": "1"}
        )
        wallet = self._wallet(currency)
        _values, paths = currency._cyclos_user_values_and_paths(wallet)
        self.assertEqual(paths, {"partner_id.city", "cyclos_login"})

    ## Sending to Cyclos

    def test_creation_sends_mapped_fields(self):
        currency = self._currency(
            {"city": "partner_id.city", "name": '"Mapped name"'}
        )
        partner = self.env["res.partner"].create(
            {"name": "New holder", "email": "new@example.test", "city": "Nantes"}
        )
        cyclos = FakeCyclosUsers()
        with patch(REQUEST_PATH, side_effect=cyclos):
            partner.cyclos_add_user(currency)
        (payload,) = cyclos.sent("post", "/users")
        self.assertEqual(payload["customValues"], {"city": "Nantes"})
        self.assertEqual(payload["name"], "Mapped name")
        self.assertEqual(payload["email"], "new@example.test")

    def test_sync_updates_mapped_fields_and_keeps_the_others(self):
        currency = self._currency({"city": "partner_id.city", "email": "partner_id.email"})
        wallet = self._wallet(currency, cyclos_id="77")
        cyclos = FakeCyclosUsers(
            edit_data={
                "77": {
                    "name": "Cyclos name",
                    "username": "868256",
                    "email": "old@example.test",
                    "customValues": {"city": "Paris", "other": "kept"},
                    "version": 4,
                }
            }
        )
        with patch(REQUEST_PATH, side_effect=cyclos):
            wallet.cyclos_sync_user_fields()
        (sent,) = cyclos.sent("put", "/users/77")
        self.assertEqual(
            sent,
            {
                "name": "Cyclos name",
                "username": "868256",
                "email": "holder-77@example.test",
                "customValues": {"city": "Nantes", "other": "kept"},
                "version": 4,
            },
        )

    def test_sync_refuses_fields_cyclos_does_not_let_odoo_edit(self):
        currency = self._currency({"city": "partner_id.city", "secret": '"x"'})
        wallet = self._wallet(currency, cyclos_id="77")
        user = {"name": "n", "username": "u", "email": "e", "customValues": {}, "version": 1}
        cyclos = FakeCyclosUsers(edit_data={"77": user})
        with patch(REQUEST_PATH, side_effect=cyclos):
            with self.assertRaisesRegex(UserError, "secret"):
                wallet.cyclos_sync_user_fields()
        self.assertFalse(cyclos.sent("put", "/users/77"))

    def test_creation_refuses_fields_cyclos_does_not_let_odoo_set(self):
        currency = self._currency({"secret": '"x"'})
        partner = self.env["res.partner"].create(
            {"name": "New holder", "email": "new@example.test"}
        )
        cyclos = FakeCyclosUsers()
        with patch(REQUEST_PATH, side_effect=cyclos):
            with self.assertRaisesRegex(UserError, "secret"):
                partner.cyclos_add_user(currency)
        self.assertFalse(cyclos.sent("post", "/users"))

    def test_sync_all_continues_after_a_refused_wallet(self):
        currency = self._currency({"city": "partner_id.city"})
        refused = self._wallet(currency, cyclos_id="1")
        accepted = self._wallet(currency, cyclos_id="2")
        user = {"name": "n", "username": "u", "email": "e", "customValues": {}, "version": 1}
        cyclos = FakeCyclosUsers(edit_data={"1": user, "2": user}, refuse={"1"})
        with patch(REQUEST_PATH, side_effect=cyclos):
            currency.cyclos_sync_all_user_fields()
        self.assertEqual(len(cyclos.sent("put", "/users/2")), 1)
        self.assertIn("refused", refused.message_ids[0].body)
        self.assertTrue(accepted.exists())
