import ast
import datetime

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval

## Fields of the Cyclos user itself; all other keys are custom fields.
CYCLOS_BASIC_USER_FIELDS = ("name", "username", "email")
## Prefix that makes a mapping value a Python expression in all cases.
EXPRESSION_PREFIX = "="
## Name of the wallet in mapping expressions.
ROOT_NAME = "record"


class RecordTracer:
    """Read-only view of records that records each field read.

    Every field read is added to ``paths`` as a dotted path from the
    wallet. Relational fields give tracers again; anything that is not a
    field (methods, private attributes) is refused, so that the recorded
    paths are all a value depends on.
    """

    def __init__(self, records, path, paths):
        object.__setattr__(self, "_records", records)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_paths", paths)

    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        records = object.__getattribute__(self, "_records")
        if name not in records._fields:
            raise ValueError(
                "only fields can be read in a mapping value: %r is not a "
                "field of %s" % (name, records._name)
            )
        path = object.__getattribute__(self, "_path")
        path = f"{path}.{name}" if path else name
        paths = object.__getattribute__(self, "_paths")
        paths.add(path)
        field = records._fields[name]
        value = records[name]
        if field.relational:
            return RecordTracer(value, path, paths)
        if value is False and field.type != "boolean":
            return None
        return value

    def __setattr__(self, name, value):
        raise AttributeError(name)

    def __iter__(self):
        records = object.__getattribute__(self, "_records")
        path = object.__getattribute__(self, "_path")
        paths = object.__getattribute__(self, "_paths")
        return iter([RecordTracer(record, path, paths) for record in records])

    def __len__(self):
        return len(object.__getattribute__(self, "_records"))

    def __bool__(self):
        return bool(object.__getattribute__(self, "_records"))

    def __repr__(self):
        return repr(object.__getattribute__(self, "_records"))


def evaluate_mapping_value(value, wallet, paths):
    """Return the value that ``value`` gives for ``wallet``.

    The rules are those of the key connector's ``MappingProxy``, with the
    wallet as record: a value naming a field of the wallet gives that
    field; otherwise, and always when it starts with ``=``, it is a Python
    expression where the fields of the wallet are names and ``record`` is
    the wallet. The fields read are added to ``paths``.
    """
    root = RecordTracer(wallet, "", paths)
    if value.startswith(EXPRESSION_PREFIX):
        expression = value[len(EXPRESSION_PREFIX) :]
    elif value in wallet._fields:
        return getattr(root, value)
    else:
        expression = value
    names = {
        node.id
        for node in ast.walk(ast.parse(expression, mode="eval"))
        if isinstance(node, ast.Name)
    }
    context = {name: getattr(root, name) for name in names if name in wallet._fields}
    if ROOT_NAME in names:
        context[ROOT_NAME] = root
    return safe_eval(expression, context)


def to_cyclos_text(value):
    """Return ``value`` as the text Cyclos expects for a user field."""
    if isinstance(value, RecordTracer):
        raise ValueError("the value is a record, not a field of it")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ValueError("Cyclos cannot receive a value of type %s" % type(value).__name__)


class CyclosUserField(models.Model):
    """A field of the Cyclos users of a currency, and how Odoo computes it."""

    _name = "res.alt.currency.cyclos.user.field"
    _description = "Cyclos user field computed by Odoo"
    _order = "currency_id, sequence, id"

    currency_id = fields.Many2one(
        "res.alt.currency", string="Currency", required=True, ondelete="cascade"
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string="Cyclos field",
        required=True,
        help="Internal name of the field of the Cyclos user: ``name``, "
        "``username``, ``email``, or a custom field.",
    )
    value = fields.Char(
        string="Value",
        required=True,
        help="Name of a field of the wallet, or a Python expression where "
        "the fields of the wallet are names and ``record`` is the wallet. "
        "A value starting with ``=`` is always a Python expression.",
    )

    _sql_constraints = [
        (
            "cyclos_field_unique",
            "unique(currency_id, name)",
            "A Cyclos field can be mapped only once per currency.",
        ),
    ]

    def _cyclos_text(self, wallet, paths):
        """Return the text of this Cyclos field for ``wallet``."""
        self.ensure_one()
        try:
            return to_cyclos_text(evaluate_mapping_value(self.value, wallet, paths))
        except Exception as e:
            raise UserError(
                _(
                    "Cannot compute the Cyclos field %(field)s of %(wallet)s "
                    "from %(value)r: %(error)s"
                )
                % {
                    "field": self.name,
                    "wallet": wallet.display_name,
                    "value": self.value,
                    "error": e,
                }
            ) from e
