from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval
import logging


_logger = logging.getLogger(__name__)


class ResPartnerBackend(models.Model):
    """Add backend commom property for local currency"""

    _name = "res.partner.backend"
    _description = (
        "Object in Odoo which match a wallet in a connected transaction backend."
    )

    type = fields.Selection([], string="Type", required=True)
    name = fields.Char("Name", required=True)
    active = fields.Boolean(default=True, tracking=True)
    partner_public_name = fields.Char(
        "Partner Public Name",
        store=True,
        compute="_compute_partner_name",
    )
    status = fields.Selection(
        [
            ("inactive", "Inactive"),
            ("to_confirm", "To Confirm"),
            ("active", "Active"),
            ("blocked", "Blocked"),
        ],
        string="Status",
        store=True,
        compute="_compute_status",
        tracking=True,
    )
    partner_id = fields.Many2one("res.partner", string="Partner", required=True)
    is_reconversion_allowed = fields.Boolean(
        "Is Reconversion Allowed?",
        readonly=True,
        compute="_compute_is_reconversion_allowed",
    )

    def _update_search_data(self, backend_keys):
        return {}

    @api.depends("name", "type")
    def _compute_status(self):
        pass

    @api.depends("partner_id.public_profile_id", "partner_id.public_profile_id.name")
    def _compute_partner_name(self):
        for record in self:
            if record.partner_id.public_profile_id:
                record.partner_public_name = record.partner_id.public_profile_id.name

    def get_lcc_product(self):
        """Return the numeric lcc product to add in sale orders or invoices.
        Need to be overrided by financial backend add-ons"""
        return None

    @api.model
    def translate_backend_key_in_wallet_name(self, backend_key):
        return backend_key

    @api.model
    def get_wallets(self, backend_keys):
        """Returns wallet objects list matching the backend_keys contents"""
        Wallet = self.env["res.partner.backend"]

        return Wallet.search(
            [
                (
                    "name",
                    "in",
                    [
                        Wallet.translate_backend_key_in_wallet_name(backend_key)
                        for backend_key in backend_keys
                    ],
                )
            ]
        )

    def get_wallet_data(self):
        """Returns wallet informations
        Need to be overrided by financial backend add-ons"""
        return []

    def credit_wallet(self, amount):
        """Send credit request to the financial backend"""
        res = {
            "success": False,
            "response": "Nothing done - Please install financial backend Odoo add-on.",
        }
        return res

    def get_wallet_balance(self):
        """Returns wallet balance
        Need to be overrided by financial backend add-ons"""
        res = {
            "success": False,
            "response": "No data - Please install financial backend Odoo add-on.",
        }
        return res

    def get_wallet_commission_rule(self):
        self.ensure_one()
        rules = self.env["commission.rule"].search(
            [("active", "=", True)], order="sequence"
        )
        return next(
            (
                rule
                for rule in rules
                if self.search(safe_eval(rule.wallet_domain) + ("id", "=", self.id))
            ),
            None,
        )

    def _compute_is_reconversion_allowed(self):
        all_rules = self.env["reconversion.rule"].search([("active", "=", True)])
        for record in self:
            # For now the reconversions are not allowed for this wallet
            record.is_reconversion_allowed = False
            if any(
                self.search(safe_eval(rule.wallet_domain) + [("id", "=", record.id)])
                and rule.is_reconversion_allowed
                for rule in all_rules
            ):
                record.is_reconversion_allowed = True
