from urllib.parse import urlparse

from odoo import models

import logging

_logger = logging.getLogger(__name__)


## Where a member logging in with Cyclos lands, instead of the website
## home page.
PORTAL_HOME = "/my/home"

## Where Odoo's ``auth_oauth`` sends a non-internal user it would
## otherwise have sent to the back office (``/web``).
ODOO_PUBLIC_LANDING = "/"


class AuthOauthProvider(models.Model):
    """Cyclos-specific knowledge about OAuth providers."""

    _inherit = "auth.oauth.provider"

    def _cyclos_landing_url(self, user, location):
        """Return where ``user`` lands after logging in with this provider.

        Odoo sends a user who is not internal to the website home page
        when no other page was asked for. After a Cyclos login, a member
        lands on their portal home instead. Any other target is kept.
        """
        self.ensure_one()
        if (
            location == ODOO_PUBLIC_LANDING
            and self._is_cyclos()
            and not user._is_internal()
        ):
            return PORTAL_HOME
        return location

    def _is_cyclos(self):
        """Whether this provider follows the Cyclos OIDC endpoint
        convention (``/api/oidc/`` in the endpoints, see ``README.md``).

        Stable across host/IP changes -- unlike a domain match against a
        currency, which is only used afterwards to pick *which* currency
        the login maps to (see ``_cyclos_alt_currency``).
        """
        self.ensure_one()
        return any(
            endpoint and "/api/oidc/" in endpoint
            for endpoint in (
                self.auth_endpoint,
                self.validation_endpoint,
                self.data_endpoint,
            )
        )

    def _cyclos_alt_currency(self):
        """Return the ``cyclos`` ``res.alt.currency`` whose server domain
        matches this provider's OIDC endpoints, or an empty recordset."""
        self.ensure_one()
        endpoint = (
            self.data_endpoint
            or self.validation_endpoint
            or self.auth_endpoint
            or ""
        )
        domain = urlparse(endpoint).netloc.lower()
        if not domain:
            return self.env["res.alt.currency"]
        currencies = self.env["res.alt.currency"].sudo().search(
            [("engine", "=", "cyclos")]
        )
        for currency in currencies:
            try:
                if currency.get_cyclos_server_domain().lower() == domain:
                    return currency
            except Exception:  # noqa: BLE001 - skip misconfigured currencies
                continue
        return self.env["res.alt.currency"]
