# -*- coding: utf-8 -*-
from odoo import models


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def reconcile(self):
        """
        This is the reliable hook: invoice becomes paid when lines get reconciled.
        After reconciliation, process commission + ledger.
        """
        # invoices potentially affected BEFORE reconcile
        moves_before = self.mapped("move_id").filtered(lambda m: m.move_type in ("out_invoice", "out_refund"))

        res = super().reconcile()

        # invoices affected AFTER reconcile (payment_state may change now)
        moves_after = (moves_before | self.mapped("move_id")).filtered(
            lambda m: m.move_type in ("out_invoice", "out_refund")
        )

        # avoid recursion if our processing causes further writes
        moves_after = moves_after.with_context(pa_v1_from_reconcile=True)

        # process only those that are now paid+posted
        to_process = moves_after.filtered(lambda m: m.state == "posted" and m.payment_state == "paid")
        if to_process:
            to_process._pa_v1_process_if_paid()

        return res