# -*- coding: utf-8 -*-
from odoo import fields, models


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    aps_on_hold = fields.Boolean(
        string='On hold for APS',
        copy=False,
        help='APS leaves this manufacturing order and its sub-assemblies out of the plan, '
             'even when work on it has started',
    )
