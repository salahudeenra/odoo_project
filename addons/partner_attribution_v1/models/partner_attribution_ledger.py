# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PartnerAttributionLedger(models.Model):
    _name = "partner.attribution.ledger"
    _description = "Partner Attribution Ledger"
    _order = "id desc"
    _rec_name = "display_name"

    display_name = fields.Char(string="Reference", compute="_compute_display_name", store=True)

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one("res.currency", related="company_id.currency_id", store=True, readonly=True)

    partner_id = fields.Many2one("res.partner", string="Attributed Partner", required=True, index=True)

    invoice_id = fields.Many2one(
        "account.move",
        string="Customer Invoice/Refund",
        required=True,
        index=True,
        ondelete="restrict",
    )
    origin_invoice_id = fields.Many2one(
        "account.move",
        string="Origin Invoice (if refund)",
        index=True,
        ondelete="restrict",
    )

    entry_type = fields.Selection(
        [("invoice", "Invoice"), ("refund", "Refund")],
        string="Type",
        required=True,
        default="invoice",
        index=True,
    )

    # payout basis
    commission_rate_used = fields.Float(string="Commission Rate Used (%)", readonly=True)
    commission_amount = fields.Monetary(string="Commission Amount (Signed)", readonly=True)

    state = fields.Selection(
        [("on_hold", "On Hold"), ("payable", "Payable"), ("paid", "Paid"), ("reversed", "Reversed")],
        string="Status",
        required=True,
        default="on_hold",
        index=True,
    )

    invoice_paid_at = fields.Datetime(string="Invoice Paid At", readonly=True)
    created_at = fields.Datetime(string="Created At", default=fields.Datetime.now, readonly=True)

    vendor_bill_id = fields.Many2one(
        "account.move",
        string="Vendor Bill",
        index=True,
        ondelete="restrict",
        readonly=True,
        copy=False,
    )
    vendor_bill_payment_state = fields.Selection(related="vendor_bill_id.payment_state", store=False, readonly=True)

    payout_batch_id = fields.Many2one(
        "partner.attribution.payout.batch",
        string="Payout Batch",
        index=True,
        ondelete="set null",
        readonly=True,
        copy=False,
    )

    partner_kyc_status = fields.Selection(related="partner_id.kyc_status", store=True, readonly=True)

    _sql_constraints = [
        ("uniq_invoice_ledger", "unique(invoice_id)", "A ledger line already exists for this invoice/refund."),
    ]

    @api.depends("invoice_id", "partner_id", "entry_type")
    def _compute_display_name(self):
        for rec in self:
            inv = rec.invoice_id
            rec.display_name = "%s | %s | %s" % (
                inv.name or inv.ref or _("Invoice"),
                rec.partner_id.display_name or _("Partner"),
                rec.entry_type,
            )

    def unlink(self):
        raise UserError(_("Ledger lines are audit records and cannot be deleted."))

    def write(self, vals):
        immutable = {
            "company_id", "partner_id", "invoice_id", "origin_invoice_id",
            "entry_type", "commission_rate_used", "commission_amount",
            "invoice_paid_at", "created_at",
        }
        if immutable.intersection(vals.keys()):
            raise UserError(_("Ledger lines are audit records. Core fields cannot be edited."))
        return super().write(vals)

    def action_recompute_payout_state(self):
        for line in self.sudo():
            # refund lines are always reversed
            if line.entry_type == "refund":
                line.state = "reversed"
                continue

            # nothing to pay
            if not line.commission_amount or line.commission_amount <= 0:
                line.state = "on_hold"
                continue

            # vendor bill paid => paid
            if line.vendor_bill_id and line.vendor_bill_id.payment_state == "paid":
                line.state = "paid"
                continue

            partner = line.partner_id
            kyc_ok = partner.kyc_status in ("complete", "verified")
            if (not kyc_ok) or partner.kyc_blocked or (not partner.bank_verified):
                line.state = "on_hold"
                continue

            line.state = "payable"

        return True