from odoo import http
from odoo.http import request

from ..models.wallet import PASSWORD_SETUP_ROUTE


class CyclosPasswordSetup(http.Controller):
    @http.route(
        f"{PASSWORD_SETUP_ROUTE}/<string:token>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        sitemap=False,
    )
    def cyclos_set_password(self, token, **kwargs):
        """Send the holder of the link to Cyclos, to choose their password."""
        wallet = request.env["res.partner.backend"].sudo()
        wallet = wallet._cyclos_wallet_by_setup_token(token)
        if not wallet:
            return request.not_found()
        response = request.redirect(
            wallet.cyclos_password_setup_redirect(), code=303, local=False
        )
        ## The address carries a Cyclos session of the holder.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
