from odoo import models, fields, api
import logging
import datetime
from firebase_admin import messaging

_logger = logging.getLogger(__name__)


class PushRegistration(models.Model):
    """"""

    _name = "push.registration"

    model = fields.Char(string="Model Name", required=True)
    res_id = fields.Integer(
        string="Record ID", help="ID of the target record in the database"
    )
    token = fields.Char(string="Device token")
    last_alive = fields.Datetime(string="Last time token alive")

    _firebase = None

    @api.model
    def register(self, model, res_id, token):

        registrations = self.search(
            [
                ("model", "=", model),
                ("res_id", "=", res_id),
                ("token", "=", token),
            ]
        )
        if len(registrations) == 0:
            self.create(
                [
                    {
                        "model": model,
                        "res_id": res_id,
                        "token": token,
                        "last_alive": datetime.datetime.now(),
                    }
                ]
            )
        elif len(registrations) == 1:
            registrations.write({"last_alive": datetime.datetime.now()})
        else:
            ## YYYvlab: Until unicity enforced by another mean, let's keep
            ## this.
            raise ValueError(
                "Unexpected multiple values for triplet (model, res_id, token): %r"
                % ((model, res_id, token),)
            )

        threshold = datetime.datetime.now() - datetime.timedelta(days=6*30)
        self.search([("last_alive", "<", threshold)]).unlink()

    @property
    def firebase(self):
        if self._firebase is None:
            fcm_config.initialize_app("monujo")
            self._firebase = message
        return self._firebase

    @api.model
    def notify(self, obj, title, body):
        """Notify all devices subscribed to given model, res_id"""
        model = obj._name
        res_id = obj.id
        tokens = set(
            self.search(
                [
                    ("model", "=", model),
                    ("res_id", "=", res_id),
                ]
            ).mapped("token")
        )

        message = self.firebase.Multicast(
            notification=self.firebase.Notification(title=title, body=body), tokens=tokens
        )
        response = self.firebase.send(message)
        _logger.info(
            "Successfully notified %d device(s) registered to %r with message %r"
            % (len(tokens), (model, res_id), response)
        )

    # @api.depends("name", "type", "cyclos_status")
    # def _compute_status(self):
    #     super(ResPartnerBackend, self)._compute_status()
    #     if self.type == "cyclos":
    #         if self.cyclos_status == "active":
    #             self.status = "active"
    #         elif self.cyclos_status == "blocked":
    #             self.status = "blocked"
    #         elif self.cyclos_status == "disabled":
    #             self.status = "inactive"
    #         elif self.cyclos_status == "pending":
    #             self.status = "to_confirm"
    #         else:
    #             self.status = ""
