# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    partner_commission_amount = fields.Monetary(
        string="Partner Commission",
        currency_field="currency_id",
        readonly=True,
        copy=False,
        help="Computed commission amount created into vendor bill when invoice is posted.",
    )
    partner_commission_bill_id = fields.Many2one(
        "account.move",
        string="Commission Vendor Bill",
        readonly=True,
        copy=False,
    )

    def _find_partner_for_commission(self):
        """
        Find partner from your existing attribution fields.
        We try fields safely (only if they exist).
        """
        self.ensure_one()
        Partner = self.env["res.partner"].sudo()

        # Try direct link field if your project has it (example: partner_attribution_partner_id)
        for fname in ["partner_attribution_partner_id", "attribution_partner_id", "partner_id_attribution"]:
            if fname in self._fields and getattr(self, fname):
                return getattr(self, fname)

        # Try partner_code on invoice if exists
        for code_field in ["partner_code", "attribution_partner_code", "x_partner_code"]:
            if code_field in self._fields:
                code = (getattr(self, code_field) or "").strip()
                if code:
                    return Partner.search([("partner_code", "=", code)], limit=1)

        return False

    def _get_expense_account_for_commission(self):
        """
        Pick any valid expense account to avoid 'account_id required' errors.
        Works in CE.
        """
        Account = self.env["account.account"].sudo()
        acc = Account.search([("account_type", "=", "expense")], limit=1)
        if not acc:
            # fallback: any account (last resort)
            acc = Account.search([], limit=1)
        if not acc:
            raise UserError(_("No account found to book commission line. Configure chart of accounts."))
        return acc

    def _create_commission_vendor_bill(self, partner, amount):
        """
        Create Vendor Bill payable to partner for the commission.
        """
        self.ensure_one()
        if amount <= 0:
            return False

        # Must have a vendor bill journal
        journal = self.env["account.journal"].sudo().search([("type", "=", "purchase")], limit=1)
        if not journal:
            raise UserError(_("No Purchase Journal found. Create one to generate vendor bills."))

        expense_account = self._get_expense_account_for_commission()

        bill = self.env["account.move"].sudo().create({
            "move_type": "in_invoice",
            "partner_id": partner.id,
            "invoice_date": fields.Date.context_today(self),
            "journal_id": journal.id,
            "ref": f"Commission for {self.name or self.ref or self.id}",
            "invoice_line_ids": [(0, 0, {
                "name": f"Partner Commission - Invoice {self.name or self.id}",
                "quantity": 1.0,
                "price_unit": float(amount),
                "account_id": expense_account.id,
            })],
        })
        return bill

    def action_post(self):
        res = super().action_post()

        for move in self:
            # Only customer invoices/refunds
            if move.move_type not in ("out_invoice", "out_refund"):
                continue

            # Only once
            if move.partner_commission_bill_id:
                continue

            partner = move._find_partner_for_commission()
            if not partner:
                continue

            rate = float(getattr(partner, "commission_rate", 0.0) or 0.0) / 100.0
            if rate <= 0:
                continue

            base_amount = abs(move.amount_untaxed)  # simple + auditable
            commission = round(base_amount * rate, 2)
            if commission <= 0:
                continue

            bill = move._create_commission_vendor_bill(partner, commission)
            if bill:
                move.sudo().write({
                    "partner_commission_amount": commission,
                    "partner_commission_bill_id": bill.id,
                })

        return res