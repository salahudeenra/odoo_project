# -*- coding: utf-8 -*-
import base64
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PartnerAttributionPayoutBatch(models.Model):
    _name = "partner.attribution.payout.batch"
    _description = "Partner Payout Batch"
    _order = "id desc"
    _rec_name = "name"

    name = fields.Char(default=lambda self: _("New"), required=True, copy=False)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)

    state = fields.Selection(
        [("draft", "Draft"), ("generated", "Vendor Bills Generated"), ("done", "Done")],
        default="draft",
        required=True,
        index=True,
    )

    # New correct field (One2many)
    ledger_line_ids = fields.One2many(
        "partner.attribution.ledger",
        "payout_batch_id",
        string="Ledger Lines",
        readonly=True,
        copy=False,
    )

    # Compatibility alias
    line_ids = fields.One2many(
        "partner.attribution.ledger",
        "payout_batch_id",
        string="Ledger Lines (alias)",
        readonly=True,
        copy=False,
    )

    vendor_bill_ids = fields.One2many(
        "account.move",
        "partner_payout_batch_id",
        string="Vendor Bills",
        readonly=True
    )

    @api.model
    def create(self, vals):
        if vals.get("name") == _("New"):
            vals["name"] = self.env["ir.sequence"].next_by_code("partner.attribution.payout.batch") or _("New")
        return super().create(vals)

    def action_load_payables(self):
        for batch in self:
            Ledger = self.env["partner.attribution.ledger"].sudo()

            # refresh current batch lines
            if batch.ledger_line_ids:
                batch.ledger_line_ids.sudo().write({"payout_batch_id": False})

            candidates = Ledger.search([
                ("company_id", "=", batch.company_id.id),
                ("entry_type", "=", "invoice"),
                ("vendor_bill_id", "=", False),
                ("state", "in", ["on_hold", "payable"]),
                ("payout_batch_id", "=", False),
            ])

            if candidates:
                candidates.action_recompute_payout_state()

            # only payables with positive commission
            payables = candidates.filtered(lambda l: l.state == "payable" and (l.commission_amount or 0.0) > 0.0)

            if payables:
                payables.sudo().write({"payout_batch_id": batch.id})
                return True

            on_hold = candidates.filtered(lambda l: l.state == "on_hold")
            already_billed = Ledger.search_count([
                ("company_id", "=", batch.company_id.id),
                ("entry_type", "=", "invoice"),
                ("vendor_bill_id", "!=", False),
            ])

            raise UserError(_(
                "No PAYABLE ledger lines found.\n\n"
                "Candidates checked: %s\n"
                "Still ON HOLD after recompute: %s\n"
                "Already billed (vendor_bill linked): %s\n\n"
                "Most common causes:\n"
                "- Invoice is not fully PAID (ledger not created)\n"
                "- Invoice has no Attributed Partner\n"
                "- Partner KYC is not verified/complete\n"
                "- Partner bank_verified is False\n"
                "- Partner is KYC blocked\n"
                "- Commission amount is 0\n"
            ) % (len(candidates), len(on_hold), already_billed))

        return True

    def _get_commission_product(self):
        param = self.env["ir.config_parameter"].sudo().get_param("partner_attribution_v1.commission_product_id")
        if not param:
            raise UserError(_("Missing config: partner_attribution_v1.commission_product_id (System Parameters)."))
        product = self.env["product.product"].browse(int(param))
        if not product.exists():
            raise UserError(_("Configured commission product not found."))
        return product

    def _get_vendor_bill_journal(self):
        param = self.env["ir.config_parameter"].sudo().get_param("partner_attribution_v1.vendor_bill_journal_id")
        if not param:
            return False
        journal = self.env["account.journal"].browse(int(param))
        return journal if journal.exists() else False

    def action_generate_vendor_bills(self):
        for batch in self:
            if batch.state != "draft":
                continue

            if not batch.ledger_line_ids:
                raise UserError(_("No payable ledger lines loaded. Click 'Load Payables' first."))

            # recompute again before generating
            batch.ledger_line_ids.action_recompute_payout_state()

            # only payable & positive
            lines_all = batch.ledger_line_ids.filtered(
                lambda l: l.state == "payable" and (l.commission_amount or 0.0) > 0.0
            )
            if not lines_all:
                raise UserError(_("No PAYABLE ledger lines with positive commission found."))

            bad = lines_all.filtered(lambda l: l.partner_id.kyc_status not in ("verified", "complete"))
            if bad:
                raise UserError(_("Some lines are not KYC verified/complete. Vendor bills cannot be generated."))

            blocked = lines_all.filtered(lambda l: l.partner_id.kyc_blocked)
            if blocked:
                raise UserError(_("Some partners are KYC-blocked. Vendor bills cannot be generated."))

            unverified_bank = lines_all.filtered(lambda l: not l.partner_id.bank_verified)
            if unverified_bank:
                raise UserError(_("Some partners do not have Bank Verified. Vendor bills cannot be generated."))

            product = batch._get_commission_product()
            journal = batch._get_vendor_bill_journal()

            by_partner = {}
            for line in lines_all:
                by_partner.setdefault(line.partner_id.id, self.env["partner.attribution.ledger"])
                by_partner[line.partner_id.id] |= line

            for partner_id, lines in by_partner.items():
                partner = lines[0].partner_id

                # ledger has commission_amount (NOT amount_total)
                total = sum(lines.mapped("commission_amount")) or 0.0
                if total <= 0.0:
                    continue

                move_vals = {
                    "move_type": "in_invoice",
                    "partner_id": partner.id,
                    "company_id": batch.company_id.id,
                    "invoice_date": fields.Date.context_today(self),
                    "ref": batch.name,
                    "partner_payout_batch_id": batch.id,
                    "invoice_line_ids": [(0, 0, {
                        "product_id": product.id,
                        "name": _("Partner commission payout (%s)") % batch.name,
                        "quantity": 1.0,
                        "price_unit": total,
                    })],
                }
                if journal:
                    move_vals["journal_id"] = journal.id

                bill = self.env["account.move"].sudo().create(move_vals)

                # link ledger lines to bill
                lines.sudo().write({"vendor_bill_id": bill.id})

                # statement uses commission_amount
                content = "\n".join([
                    "Payout Batch: %s" % batch.name,
                    "Partner: %s" % partner.display_name,
                    "Lines:",
                    *["- %s = %s" % (l.display_name, l.commission_amount) for l in lines],
                    "TOTAL = %s" % total,
                ]).encode("utf-8")

                self.env["ir.attachment"].sudo().create({
                    "name": "Payout Statement - %s - %s.txt" % (batch.name, partner.display_name),
                    "type": "binary",
                    "datas": base64.b64encode(content),
                    "res_model": "account.move",
                    "res_id": bill.id,
                    "mimetype": "text/plain",
                })

            batch.state = "generated"
            batch.ledger_line_ids.action_recompute_payout_state()

        return True

    def action_sync_paid_status(self):
        for batch in self:
            if not batch.vendor_bill_ids:
                continue

            for bill in batch.vendor_bill_ids:
                if bill.payment_state == "paid":
                    lines = self.env["partner.attribution.ledger"].sudo().search([("vendor_bill_id", "=", bill.id)])
                    lines.sudo().write({"state": "paid"})

            batch.ledger_line_ids.action_recompute_payout_state()

            if batch.vendor_bill_ids and all(b.payment_state == "paid" for b in batch.vendor_bill_ids):
                batch.state = "done"

        return True