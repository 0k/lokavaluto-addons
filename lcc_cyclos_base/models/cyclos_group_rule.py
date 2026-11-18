from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.safe_eval import safe_eval


class CyclosGroupRule(models.Model):
    """A rule choosing the Cyclos group of the user of a new wallet.

    Odoo creates the Cyclos user of a new wallet in the group of the first
    active rule of the currency, by priority, whose partner domain matches
    the holder. Users already created are never moved.
    """

    _name = "cyclos.group.rule"
    _inherit = ["mail.thread"]
    _description = "Cyclos group rule"
    _order = "alt_currency_id, sequence, id"

    alt_currency_id = fields.Many2one(
        "res.alt.currency",
        string="Currency",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(
        string="Priority",
        default=10,
        help="Rules are tried by increasing priority; the first rule whose "
        "domain matches the holder gives the group.",
        tracking=True,
    )
    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    partner_domain = fields.Char(
        string="Holders",
        default="[]",
        required=True,
        help="Domain on the holder (contact) of the new wallet.",
        tracking=True,
    )
    cyclos_group = fields.Char(
        string="Cyclos group",
        required=True,
        help="Internal name of the Cyclos group in which Odoo creates the "
        "user (``GET /api/users/groups-for-registration`` lists them).",
        tracking=True,
    )

    @api.constrains("partner_domain", "cyclos_group")
    def _check_rule(self):
        for rule in self:
            if not rule.cyclos_group.strip():
                raise ValidationError(
                    _("The Cyclos group of rule %s is empty.") % rule.name
                )
            try:
                self.env["res.partner"].sudo().search(rule._partner_domain(), limit=1)
            except (ValueError, SyntaxError) as e:
                raise ValidationError(
                    _("The holder domain of rule %s is invalid: %s") % (rule.name, e)
                ) from e

    def _partner_domain(self):
        self.ensure_one()
        domain = safe_eval(self.partner_domain)
        if not isinstance(domain, list):
            raise ValueError(_("a domain is a list, not %r") % (domain,))
        return domain

    def _matching_partners(self, partners):
        """Return those of ``partners`` that the domain of the rule matches."""
        domain = self._partner_domain()
        return (
            self.env["res.partner"]
            .sudo()
            .with_context(active_test=False)
            .search(domain + [("id", "in", partners.ids)])
        )
