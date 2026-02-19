# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

try:
    from odoo.http import request
except Exception:
    request = None

COOKIE_NAME = "partner_code"
SESSION_KEY = "partner_code"


class AccountMove(models.Model):
    _inherit = "account.move"

    # ----------------------------
    # Attribution fields
    # ----------------------------
    attributed_partner_id = fields.Many2one(
        "res.partner",
        string="Attributed Partner",
        readonly=False,
        copy=False,
        index=True,
        help="Attribution copied from Sales Order (locked on post) or set from referral cookie/session on creation.",
        domain=[("partner_state", "=", "approved")],
    )
    attribution_locked = fields.Boolean(string="Attribution Locked", default=False, copy=False)
    attribution_locked_at = fields.Datetime(string="Attribution Locked At", readonly=True, copy=False)
    attribution_locked_by = fields.Many2one("res.users", string="Attribution Locked By", readonly=True, copy=False)

    partner_payout_batch_id = fields.Many2one(
        "partner.attribution.payout.batch",
        string="Partner Payout Batch",
        copy=False,
        index=True,
        ondelete="set null",
    )

    # ----------------------------
    # Commission / Vendor bill
    # ----------------------------
    commission_vendor_bill_id = fields.Many2one(
        "account.move",
        string="Commission Vendor Bill",
        readonly=True,
        copy=False,
        help="Vendor Bill created for this invoice commission.",
    )

    commission_rate_used = fields.Float(
        string="Commission Rate Used (%)",
        compute="_compute_commission_values",
        store=True,
        readonly=True,
        help="Snapshot of commission rate used for commission calculation.",
    )

    commission_amount = fields.Monetary(
        string="Commission Amount",
        currency_field="currency_id",
        compute="_compute_commission_values",
        store=True,
        readonly=True,
    )

    commission_bill_state = fields.Selection(
        [("none", "No Commission"), ("pending", "Pending"), ("billed", "Billed")],
        string="Commission Status",
        compute="_compute_commission_bill_state",
        store=True,
        readonly=True,
    )

    # ----------------------------
    # Referral helper
    # ----------------------------
    def _find_partner_by_code(self, code):
        code = (code or "").strip()
        if not code:
            return self.env["res.partner"]
        return self.env["res.partner"].sudo().search(
            [("partner_code", "=", code), ("partner_state", "=", "approved")],
            limit=1,
        )

    def _get_referral_code_from_http(self):
        if not request:
            return False
        code = (request.session.get(SESSION_KEY) or "").strip()
        if code:
            return code
        try:
            return (request.httprequest.cookies.get(COOKIE_NAME) or "").strip()
        except Exception:
            return False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("attributed_partner_id"):
                continue

            move_type = vals.get("move_type")
            if move_type not in ("out_invoice", "out_refund"):
                continue

            ref_code = (self._get_referral_code_from_http() or "").strip()
            if ref_code:
                partner = self._find_partner_by_code(ref_code)
                if partner:
                    vals["attributed_partner_id"] = partner.id

        moves = super().create(vals_list)
        moves._pa_v1_process_if_paid()
        return moves

    # ----------------------------
    # Commission compute
    # ----------------------------
    @api.depends("attributed_partner_id", "amount_untaxed", "currency_id", "move_type")
    def _compute_commission_values(self):
        for move in self:
            rate = 0.0
            if move.attributed_partner_id:
                rate = float(move.attributed_partner_id.commission_rate or 0.0)

            move.commission_rate_used = rate

            amt = (move.amount_untaxed or 0.0) * (rate / 100.0) if rate else 0.0
            if move.move_type == "out_refund" and amt:
                amt = -abs(amt)

            move.commission_amount = amt

    @api.depends("commission_vendor_bill_id", "commission_amount")
    def _compute_commission_bill_state(self):
        for move in self:
            if not move.commission_amount:
                move.commission_bill_state = "none"
            elif move.commission_vendor_bill_id:
                move.commission_bill_state = "billed"
            else:
                move.commission_bill_state = "pending"

    # ----------------------------
    # Commission Bill helpers
    # ----------------------------
    def _pa_v1_get_commission_expense_account(self):
        """Odoo 17 safe: use company context + search valid expense types."""
        self.ensure_one()
        company = self.company_id or self.env.company
        Account = self.env["account.account"].sudo()

        acc = Account.search([
            ("company_id", "=", company.id),
            ("deprecated", "=", False),
            ("account_type", "in", ("expense", "expense_direct_cost")),
        ], limit=1)

        if acc:
            return acc

        raise UserError(_(
            "No Expense account found to book Commission Vendor Bills.\n\n"
            "Fix:\n"
            "Accounting → Configuration → Chart of Accounts\n"
            "Create at least one account with Type = Expense (or Direct Costs)."
        ))

    def _pa_v1_should_create_commission_bill(self):
        self.ensure_one()
        return bool(
            self.move_type == "out_invoice"
            and self.state == "posted"
            and self.payment_state == "paid"
            and self.attributed_partner_id
            and (self.commission_amount or 0.0) > 0.0
            and not self.commission_vendor_bill_id
        )

    def _pa_v1_create_commission_vendor_bill(self, autopost=True):
        self.ensure_one()
        if not self._pa_v1_should_create_commission_bill():
            return self.commission_vendor_bill_id or False

        company = self.company_id or self.env.company
        partner = self.attributed_partner_id.commercial_partner_id.with_company(company)
        expense_acc = self._pa_v1_get_commission_expense_account()
        ref_name = self.name or self.payment_reference or str(self.id)

        # 1) Ensure vendor payable account exists (Community-safe: auto-fallback)
        payable = partner.property_account_payable_id
        if not payable:
            default_payable = company.account_payable_id
            if not default_payable:
                raise UserError(_(
                    "Cannot create Commission Vendor Bill because no payable account is configured.\n\n"
                    "Fix one of these:\n"
                    "• Set a payable account on the vendor (property_account_payable_id)\n"
                    "• Or set a default payable account on the company (account_payable_id)\n\n"
                    "Company: %s"
                ) % (company.display_name,))
            # IMPORTANT: write in company context
            partner.sudo().with_company(company).property_account_payable_id = default_payable

        # 2) Pick a purchase journal explicitly
        journal = self.env["account.journal"].sudo().search([
            ("type", "=", "purchase"),
            ("company_id", "=", company.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "Cannot create Commission Vendor Bill because no Purchase Journal exists for company '%s'."
            ) % (company.display_name,))

        bill_vals = {
            "move_type": "in_invoice",
            "partner_id": partner.id,
            "company_id": company.id,
            "journal_id": journal.id,
            "invoice_date": fields.Date.context_today(self),
            "ref": _("Commission for %s") % ref_name,
            "invoice_origin": self.name or "",
            "invoice_line_ids": [(0, 0, {
                "name": _("Commission for Invoice %s") % ref_name,
                "quantity": 1.0,
                "price_unit": float(self.commission_amount or 0.0),
                "account_id": expense_acc.id,
            })],
        }

        bill = self.env["account.move"].sudo().with_company(company).create(bill_vals)

        # 3) Extra safety: recompute dynamic lines
        try:
            bill._recompute_dynamic_lines(recompute_all_taxes=True)
        except Exception:
            pass

        if autopost:
            try:
                bill.action_post()
            except Exception as e:
                raise UserError(_(
                    "Commission Vendor Bill was created but could not be posted.\n\n"
                    "Bill: %s\nError: %s"
                ) % (bill.display_name, str(e)))

        self.sudo().write({"commission_vendor_bill_id": bill.id})
        return bill

    def action_create_commission_bill(self):
        for move in self:
            if move.move_type != "out_invoice":
                raise UserError(_("Commission bill can only be created from a Customer Invoice."))
            if move.state != "posted":
                raise UserError(_("Please post the Customer Invoice first."))
            if move.payment_state != "paid":
                raise UserError(_("Commission bill can only be created after the invoice is PAID."))
            if not move.attributed_partner_id:
                raise UserError(_("This invoice has no Attributed Partner."))
            if (move.commission_amount or 0.0) <= 0.0:
                raise UserError(_("Commission amount is 0. Nothing to bill."))
            if move.commission_vendor_bill_id:
                continue

            move._pa_v1_create_commission_vendor_bill(autopost=True)

        if len(self) == 1 and self.commission_vendor_bill_id:
            return {
                "type": "ir.actions.act_window",
                "name": _("Commission Vendor Bill"),
                "res_model": "account.move",
                "res_id": self.commission_vendor_bill_id.id,
                "view_mode": "form",
                "target": "current",
            }
        return True

    # ----------------------------
    # Lock behavior (lock on POST)
    # ----------------------------
    def _lock_attribution(self):
        for move in self:
            if move.attribution_locked:
                continue
            if not move.attributed_partner_id:
                raise ValidationError(_("Cannot lock invoice attribution without an Attributed Partner."))

            locked_by = move.attribution_locked_by.id if move.attribution_locked_by else self.env.user.id

            move.sudo().write({
                "attribution_locked": True,
                "attribution_locked_at": move.attribution_locked_at or fields.Datetime.now(),
                "attribution_locked_by": locked_by,
            })

    # ----------------------------
    # Ledger creation rules
    # ----------------------------
    def _should_create_partner_ledger(self):
        self.ensure_one()
        return bool(
            self.attributed_partner_id
            and self.move_type in ("out_invoice", "out_refund")
            and self.state == "posted"
            and self.payment_state == "paid"
        )

    def _create_partner_ledger_if_needed(self, paid_at=None):
        Ledger = self.env["partner.attribution.ledger"].sudo()

        for move in self:
            if not move._should_create_partner_ledger():
                continue

            if Ledger.search_count([("invoice_id", "=", move.id)]) > 0:
                continue

            entry_type = "refund" if move.move_type == "out_refund" else "invoice"
            origin = move.reversed_entry_id if entry_type == "refund" else False

            Ledger.create({
                "company_id": move.company_id.id,
                "partner_id": move.attributed_partner_id.id,
                "invoice_id": move.id,
                "origin_invoice_id": origin.id if origin else False,
                "entry_type": entry_type,
                "commission_rate_used": float(move.commission_rate_used or 0.0),
                "commission_amount": float(move.commission_amount or 0.0),
                "state": "on_hold",
                "invoice_paid_at": paid_at or fields.Datetime.now(),
            })

    # ----------------------------
    # SAFE paid-processing
    # ----------------------------
    def _pa_v1_process_if_paid(self):
        if self.env.context.get("pa_v1_processing"):
            return

        self = self.with_context(pa_v1_processing=True)
        Ledger = self.env["partner.attribution.ledger"].sudo()

        for move in self:
            if not move._should_create_partner_ledger():
                continue

            move._create_partner_ledger_if_needed()

            bill = False
            if move._pa_v1_should_create_commission_bill():
                bill = move._pa_v1_create_commission_vendor_bill(autopost=True)

            if bill:
                ledger = Ledger.search([("invoice_id", "=", move.id)], limit=1)
                if ledger and not ledger.vendor_bill_id:
                    ledger.write({"vendor_bill_id": bill.id})

            ledger_lines = Ledger.search([("invoice_id", "=", move.id)])
            if ledger_lines:
                ledger_lines.action_recompute_payout_state()

    # ----------------------------
    # Allow editing on draft, prevent changes after lock
    # ----------------------------
    def write(self, vals):
        vals = dict(vals)
        locked_fields = {"attributed_partner_id", "attribution_locked", "attribution_locked_at", "attribution_locked_by"}

        for move in self:
            if "attributed_partner_id" in vals and move.state != "draft":
                raise UserError(_("You can only change Attributed Partner while the invoice is in Draft."))
            if move.attribution_locked and locked_fields.intersection(vals.keys()):
                raise UserError(_("Invoice attribution is locked and cannot be changed."))

        before_paid = {m.id: (m.payment_state == "paid" and m.state == "posted") for m in self}
        res = super().write(vals)

        to_process = self.filtered(lambda m: (m.state == "posted" and m.payment_state == "paid" and not before_paid.get(m.id)))
        if to_process:
            to_process._pa_v1_process_if_paid()

        return res

    def action_post(self):
        res = super().action_post()

        to_lock = self.filtered(lambda m: m.state == "posted" and m.attributed_partner_id and not m.attribution_locked)
        if to_lock:
            to_lock._lock_attribution()

        to_process = self.filtered(lambda m: m.state == "posted" and m.payment_state == "paid")
        if to_process:
            to_process._pa_v1_process_if_paid()

        return res