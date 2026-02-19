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

    ledger_line_ids = fields.One2many(
        "partner.attribution.ledger",
        "payout_batch_id",
        string="Ledger Lines",
        readonly=True,
        copy=False,
    )

    # Compatibility alias (kept for views/old code)
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
        readonly=True,
    )

    @api.model
    def create(self, vals):
        if vals.get("name") in (False, _("New"), "New"):
            vals["name"] = self.env["ir.sequence"].next_by_code("partner.attribution.payout.batch") or _("New")
        return super().create(vals)

    # ----------------------------
    # Helpers
    # ----------------------------
    def _get_commission_product(self):
        param = self.env["ir.config_parameter"].sudo().get_param("partner_attribution_v1.commission_product_id")
        if not param:
            raise UserError(_("Missing config: partner_attribution_v1.commission_product_id (System Parameters)."))
        product = self.env["product.product"].browse(int(param))
        if not product.exists():
            raise UserError(_("Configured commission product not found."))
        return product

    def _get_vendor_bill_journal(self, company):
        param = self.env["ir.config_parameter"].sudo().get_param("partner_attribution_v1.vendor_bill_journal_id")
        if param:
            journal = self.env["account.journal"].browse(int(param))
            if journal.exists():
                return journal

        return self.env["account.journal"].sudo().search(
            [("company_id", "=", company.id), ("type", "=", "purchase")],
            limit=1,
        )

    def _get_expense_account(self, company):
        Account = self.env["account.account"].sudo()
        acc = Account.search(
            [
                ("company_id", "=", company.id),
                ("deprecated", "=", False),
                ("account_type", "in", ("expense", "expense_direct_cost")),
            ],
            limit=1,
        )
        if not acc:
            raise UserError(_(
                "No Expense account found to book commission vendor bills.\n\n"
                "Fix:\n"
                "Accounting → Configuration → Chart of Accounts\n"
                "Create at least one account with Type = Expense (or Direct Costs)."
            ))
        return acc

    def _precheck_vendor_bill_config(self, partner, company, journal):
        if not journal:
            raise UserError(_(
                "No Purchase Journal found.\n\n"
                "Fix:\n"
                "- Create a Purchase journal for the company, OR\n"
                "- Set System Parameter: partner_attribution_v1.vendor_bill_journal_id"
            ))

        payable = getattr(partner, "property_account_payable_id", False)
        if not payable:
            raise UserError(_(
                "Partner '%s' has no Payable Account configured.\n\n"
                "Fix:\n"
                "Contacts → Partner → Accounting tab → Payable Account\n"
                "or set company default payable account."
            ) % (partner.display_name,))

    # ----------------------------
    # Actions
    # ----------------------------
    def action_load_payables(self):
        """
        Load payable ledger lines into this batch (per batch record).
        """
        Ledger = self.env["partner.attribution.ledger"].sudo()

        for batch in self:
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

            payables = candidates.filtered(lambda l: l.state == "payable" and (l.commission_amount or 0.0) > 0.0)
            if payables:
                payables.sudo().write({"payout_batch_id": batch.id})
                continue  # do NOT return early; allow multi-record batches

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

    def action_generate_vendor_bills(self):
        for batch in self:
            if batch.state != "draft":
                continue

            if not batch.ledger_line_ids:
                raise UserError(_("No payable ledger lines loaded. Click 'Load Payables' first."))

            batch.ledger_line_ids.action_recompute_payout_state()

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
            journal = batch._get_vendor_bill_journal(batch.company_id)
            expense_acc = batch._get_expense_account(batch.company_id)

            by_partner = {}
            for line in lines_all:
                by_partner.setdefault(line.partner_id.id, self.env["partner.attribution.ledger"])
                by_partner[line.partner_id.id] |= line

            Move = self.env["account.move"].sudo()

            for _partner_id, lines in by_partner.items():
                partner = lines[0].partner_id
                batch._precheck_vendor_bill_config(partner, batch.company_id, journal)

                total = sum(lines.mapped("commission_amount")) or 0.0
                if total <= 0.0:
                    continue

                bill = Move.create({
                    "move_type": "in_invoice",
                    "partner_id": partner.id,
                    "company_id": batch.company_id.id,
                    "invoice_date": fields.Date.context_today(self),
                    "ref": batch.name,
                    "partner_payout_batch_id": batch.id,
                    "journal_id": journal.id,
                    "invoice_line_ids": [(0, 0, {
                        "product_id": product.id,
                        "name": _("Partner commission payout (%s)") % batch.name,
                        "quantity": 1.0,
                        "price_unit": float(total),
                        "account_id": expense_acc.id,
                    })],
                })

                try:
                    bill.action_post()
                except Exception:
                    pass

                lines.sudo().write({"vendor_bill_id": bill.id})

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
                if getattr(bill, "payment_state", False) == "paid":
                    lines = self.env["partner.attribution.ledger"].sudo().search([("vendor_bill_id", "=", bill.id)])
                    lines.sudo().write({"state": "paid"})

            batch.ledger_line_ids.action_recompute_payout_state()

            if batch.vendor_bill_ids and all(getattr(b, "payment_state", False) == "paid" for b in batch.vendor_bill_ids):
                batch.state = "done"

        return True

    # ==========================================================
    # AUTOMATION HOOKS (Cron-safe wrappers)
    # ==========================================================
    @api.model
    def _cron_sync_payout_batches_paid_status(self):
        """
        Cron target: sync 'paid' from vendor bills back to ledger + close batches.
        Runs per company and processes in chunks to avoid missing records.
        """
        for company in self.env["res.company"].sudo().search([]):
            Batch = self.sudo().with_company(company)

            last_id = 0
            while True:
                batches = Batch.search(
                    [("state", "in", ("generated", "done")), ("id", ">", last_id)],
                    order="id asc",
                    limit=200,
                )
                if not batches:
                    break

                batches.action_sync_paid_status()
                last_id = batches[-1].id

        return True

    @api.model
    def _cron_recompute_orphan_ledger_states(self):
        """
        Cron target: re-evaluate payout state for ledger lines NOT in a batch yet.
        Runs per company and processes in chunks.
        """
        if "partner.attribution.ledger" not in self.env:
            return True

        LedgerModel = self.env["partner.attribution.ledger"].sudo()

        for company in self.env["res.company"].sudo().search([]):
            last_id = 0
            while True:
                lines = LedgerModel.search([
                    ("company_id", "=", company.id),
                    ("entry_type", "=", "invoice"),
                    ("vendor_bill_id", "=", False),
                    ("payout_batch_id", "=", False),
                    ("state", "in", ("on_hold", "payable")),
                    ("id", ">", last_id),
                ], order="id asc", limit=500)

                if not lines:
                    break

                lines.action_recompute_payout_state()
                last_id = lines[-1].id

        return True