import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests.common import Form, TransactionCase, new_test_user

from .test_res_alt_currency import (
    CYCLOS_WEB_URL,
    REQUEST_PATH,
    _registration_response,
)

PERSONS = "[('is_company', '=', False)]"
COMPANIES = "[('is_company', '=', True)]"
ASSOCIATIONS = "[('category_id.name', '=', 'Association')]"


class CyclosGroupRuleCase(TransactionCase):
    def _currency(self, rules, ident="currencyR"):
        """Create a Cyclos currency with ``rules``: (sequence, domain, group)."""
        return self.env["res.alt.currency"].create(
            {
                "name": f"Currency {ident}",
                "ident": ident,
                "engine": "cyclos",
                "currency_unit_product_id": self.env.ref(
                    "lcc_lokavaluto_app_connection.product_product_numeric_lcc"
                ).id,
                "cyclos_server_url": "https://cyclos.example.test/api",
                "cyclos_server_login": "611424",
                "cyclos_server_password": "secret",
                "cyclos_web_url": CYCLOS_WEB_URL,
                "cyclos_group_rule_ids": [
                    (
                        0,
                        0,
                        {
                            "name": group,
                            "sequence": sequence,
                            "partner_domain": domain,
                            "cyclos_group": group,
                        },
                    )
                    for sequence, domain, group in rules
                ],
            }
        )

    def _partner(self, is_company=False, tags=()):
        self._partners_created = getattr(self, "_partners_created", 0) + 1
        return self.env["res.partner"].create(
            {
                "name": "New holder",
                "is_company": is_company,
                "email": f"holder-{self._partners_created}@example.test",
                "category_id": [(0, 0, {"name": tag}) for tag in tags],
            }
        )

    def _group_for(self, currency, partner):
        return currency.cyclos_group_rule_for_partner(partner).cyclos_group


class TestCyclosGroupRuleResolution(CyclosGroupRuleCase):
    def test_partner_kind_selects_group(self):
        currency = self._currency(
            [(10, PERSONS, "individual"), (20, COMPANIES, "professional")]
        )
        self.assertEqual(self._group_for(currency, self._partner()), "individual")
        self.assertEqual(
            self._group_for(currency, self._partner(is_company=True)), "professional"
        )

    def test_first_matching_rule_by_priority_wins(self):
        currency = self._currency(
            [(20, COMPANIES, "professional"), (10, ASSOCIATIONS, "associations")]
        )
        association = self._partner(is_company=True, tags=["Association"])
        self.assertEqual(self._group_for(currency, association), "associations")
        self.assertEqual(
            self._group_for(currency, self._partner(is_company=True)), "professional"
        )

    def test_archived_rule_is_ignored(self):
        currency = self._currency(
            [(10, PERSONS, "archived-group"), (20, PERSONS, "individual")]
        )
        currency.cyclos_group_rule_ids.filtered(
            lambda r: r.cyclos_group == "archived-group"
        ).active = False
        self.assertEqual(self._group_for(currency, self._partner()), "individual")

    def test_rules_of_another_currency_are_ignored(self):
        self._currency([(1, PERSONS, "other-currency-group")], ident="other")
        currency = self._currency([(10, PERSONS, "individual")])
        self.assertEqual(self._group_for(currency, self._partner()), "individual")

    def test_invalid_partner_domain_is_refused_when_saved(self):
        for domain in ("[('no_such_field', '=', 1)]", "42", "[("):
            with self.subTest(domain=domain), self.assertRaises(ValidationError):
                self._currency([(10, domain, "individual")])

    def test_empty_group_is_refused(self):
        with self.assertRaises(ValidationError):
            self._currency([(10, PERSONS, "  ")])


class TestCyclosRegistrationGroup(CyclosGroupRuleCase):
    def test_registration_creates_user_in_group_of_matching_rule(self):
        currency = self._currency(
            [(20, COMPANIES, "professional"), (10, ASSOCIATIONS, "associations")]
        )
        partner = self._partner(is_company=True, tags=["Association"])
        response = MagicMock(text=_registration_response())
        with patch(REQUEST_PATH, return_value=response) as request:
            partner.cyclos_add_user(currency)
        (call,) = request.call_args_list
        self.assertEqual(json.loads(call.kwargs["data"])["group"], "associations")

    def test_registration_refused_when_no_rule_matches(self):
        currency = self._currency([(10, PERSONS, "individual")])
        partner = self._partner(is_company=True)
        with patch(REQUEST_PATH) as request:
            with self.assertRaisesRegex(UserError, "New holder") as raised:
                partner.cyclos_add_user(currency)
        self.assertIn(currency.display_name, str(raised.exception))
        request.assert_not_called()
        self.assertFalse(partner.lcc_backend_ids)

    def test_rule_added_from_currency_form(self):
        currency = self._currency([])
        with Form(currency) as form:
            with form.cyclos_group_rule_ids.new() as rule:
                rule.name = "Persons"
                rule.partner_domain = PERSONS
                rule.cyclos_group = "individual"
        self.assertEqual(self._group_for(currency, self._partner()), "individual")

    def test_rules_are_edited_by_currency_administrators_only(self):
        manager = new_test_user(
            self.env,
            login="wallet-manager",
            groups="base.group_user,"
            "lcc_lokavaluto_app_connection.group_wallet_full_manager",
        )
        currency = self._currency([(10, PERSONS, "individual")])
        rule = currency.cyclos_group_rule_ids
        self.assertEqual(rule.with_user(manager).cyclos_group, "individual")
        with self.assertRaises(AccessError):
            rule.with_user(manager).cyclos_group = "other"


class TestCyclosGroupRulePreview(CyclosGroupRuleCase):
    def _preview(self, currency, partner):
        return self.env["cyclos.group.rule.preview"].create(
            {"alt_currency_id": currency.id, "partner_id": partner.id}
        )

    def test_preview_shows_winning_rule_without_creating_anything(self):
        currency = self._currency(
            [(20, COMPANIES, "professional"), (10, ASSOCIATIONS, "associations")]
        )
        partner = self._partner(is_company=True, tags=["Association"])
        with patch(REQUEST_PATH) as request:
            preview = self._preview(currency, partner)
            self.assertEqual(preview.cyclos_group, "associations")
            self.assertEqual(preview.rule_id.sequence, 10)
        request.assert_not_called()
        self.assertFalse(partner.lcc_backend_ids)

    def test_preview_form_shows_group_of_chosen_holder(self):
        currency = self._currency([(10, PERSONS, "individual")])
        partner = self._partner()
        action = self.env.ref("lcc_cyclos_base.action_cyclos_group_rule_preview")
        form = Form(
            self.env[action.res_model].with_context(
                default_alt_currency_id=currency.id
            ),
            view=action.view_id,
        )
        form.partner_id = partner
        self.assertEqual(form.cyclos_group, "individual")

    def test_preview_tells_when_no_rule_matches(self):
        currency = self._currency([(10, PERSONS, "individual")])
        preview = self._preview(currency, self._partner(is_company=True))
        self.assertFalse(preview.rule_id)
        self.assertFalse(preview.cyclos_group)
        self.assertTrue(preview.message)


class TestCyclosGroupRuleMigration(TransactionCase):
    """Currencies keep the groups they had in their two group settings."""

    def _migrate(self):
        path = get_module_path("lcc_cyclos_base")
        spec = importlib.util.spec_from_file_location(
            "lcc_cyclos_base_post_migrate_16_0_1_5_0",
            os.path.join(path, "migrations", "16.0.1.5.0", "post-migrate.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "16.0.1.4.0")
        self.env.invalidate_all()

    def _old_columns(self, currency, person, company):
        """Simulate the two group columns of version 16.0.1.4.0."""
        self.env.flush_all()
        cr = self.env.cr
        for column in ("cyclos_user_group_person", "cyclos_user_group_company"):
            cr.execute(
                f"ALTER TABLE res_alt_currency ADD COLUMN IF NOT EXISTS {column} varchar"
            )
        cr.execute(
            "UPDATE res_alt_currency SET cyclos_user_group_person = %s, "
            "cyclos_user_group_company = %s WHERE id = %s",
            (person, company, currency.id),
        )

    def _column_names(self):
        self.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'res_alt_currency'"
        )
        return {row[0] for row in self.env.cr.fetchall()}

    def test_saved_groups_become_rules(self):
        currency = self.env["res.alt.currency"].create(
            {"name": "C", "ident": "cyclos.example.test", "engine": "cyclos"}
        )
        self._old_columns(currency, "individual", "professional")
        self._migrate()

        person = self.env["res.partner"].create({"name": "P"})
        company = self.env["res.partner"].create({"name": "C", "is_company": True})
        rule_for = currency.cyclos_group_rule_for_partner
        self.assertEqual(rule_for(person).cyclos_group, "individual")
        self.assertEqual(rule_for(company).cyclos_group, "professional")
        self.assertFalse(
            {"cyclos_user_group_person", "cyclos_user_group_company"}
            & self._column_names()
        )
