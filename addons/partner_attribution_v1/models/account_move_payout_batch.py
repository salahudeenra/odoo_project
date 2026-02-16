# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    partner_payout_batch_id = fields.Many2one(
        "partner.attribution.payout.batch",
        string="Payout Batch",
        copy=False,
        index=True,
        readonly=True,
    )