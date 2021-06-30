import requests
import json
from requests.auth import HTTPBasicAuth
from odoo import models, fields, api
import logging
_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    ''' Inherits partner, adds comchain fields in the partner form, and functions'''
    _inherit = 'res.partner'

    comchain_active = fields.Boolean(string='comchain OK')
    comchain_id = fields.Char(string="Address")
    comchain_wallet = fields.Text(string="Crypted json wallet")
    comchain_status = fields.Char(string="Comchain Status")
    comchain_type = fields.Selection([
        ('0', 'Personal'),
        ('1', 'Company'),
        ('2', 'Admin')
    ], string='Type')
    comchain_credit_min = fields.Float(string="Min Credit limit")
    comchain_credit_max = fields.Float(string="Max Credit limit")

    @api.multi
    def open_commercial_member_entity(self):
        """ Utility method used to add an "Open Company" button in partner views """
        self.ensure_one()
        company_form_id = self.env.ref('lcc_members.main_members_view').id
        return {'type': 'ir.actions.act_window',
                'res_model': 'res.partner',
                'view_mode': 'form',
                'views': [(company_form_id, 'form')],
                'res_id': self.commercial_partner_id.id,
                'target': 'current',
                'flags': {'form': {'action_buttons': True}}}

    def _update_auth_data(self, password):
        self.ensure_one()
        data = super(ResPartner, self)._update_auth_data(password)
        # Update comchain password with odoo one from authenticate session
        if self.comchain_id:
            comchain_data = {
                'type': 'comchain',
                'address': self.comchain_id,
                'wallet': '%s' % json.loads(self.comchain_wallet),
            }
            _logger.debug('NEW TOKEN: comchain_data %s' % comchain_data)
            data.append(comchain_data)
            _logger.debug('NEW TOKEN: data %s' % data)
            return data
        return {}

    @api.multi
    def validatecomchainUser(self):
        for record in self:
            record.write({
                'comchain_active': True,
                'comchain_status': "actif",
            })