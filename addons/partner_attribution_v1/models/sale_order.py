# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

try:
    from odoo.http import request
except Exception:
    request = None

COOKIE_NAME = "partner_code"
SESSION_KEY = "partner_code"


class SaleOrder(models.Model):
    _inherit = "sale.order"

    partner_code_input = fields.Char(string="Partner Code", copy=False)

    attributed_partner_id = fields.Many2one(
        "res.partner",
        string="Attributed Partner",
        copy=False,
        index=True,
        domain=[("partner_state", "=", "approved")],
    )

    attribution_locked = fields.Boolean(string="Attribution Locked", default=False, copy=False)
    attribution_locked_at = fields.Datetime(string="Attribution Locked At", copy=False, readonly=True)
    attribution_locked_by = fields.Many2one("res.users", string="Locked By", copy=False, readonly=True)

    # ----------------------------
    # Helpers
    # ----------------------------
    def _find_partner_by_code(self, code):
        """Lookup an approved partner by code. sudo() to be safe for website/portal contexts."""
        code = (code or "").strip()
        if not code:
            return self.env["res.partner"]
        return self.env["res.partner"].sudo().search(
            [("partner_code", "=", code), ("partner_state", "=", "approved")],
            limit=1,
        )

    def _get_referral_code_from_http(self):
        """Read referral code from website session/cookie (if request context exists)."""
        if not request:
            return False
        code = (request.session.get(SESSION_KEY) or "").strip()
        if code:
            return code
        try:
            return (request.httprequest.cookies.get(COOKIE_NAME) or "").strip()
        except Exception:
            return False

    def _sync_code_from_attributed_partner(self, vals):
        if "attributed_partner_id" in vals and "partner_code_input" not in vals:
            partner = (
                self.env["res.partner"].browse(vals["attributed_partner_id"])
                if vals.get("attributed_partner_id")
                else self.env["res.partner"]
            )
            vals["partner_code_input"] = partner.partner_code or False

    def _sync_attributed_partner_from_code(self, vals):
        if "partner_code_input" in vals and "attributed_partner_id" not in vals:
            code = (vals.get("partner_code_input") or "").strip()
            if not code:
                vals["attributed_partner_id"] = False
                return
            partner = self._find_partner_by_code(code)
            vals["attributed_partner_id"] = partner.id if partner else False

    # ----------------------------
    # Buttons
    # ----------------------------
    def action_lock_attribution(self):
        for order in self:
            if order.attribution_locked:
                continue
            order.with_context(bypass_attribution_lock=True).write({
                "attribution_locked": True,
                "attribution_locked_at": fields.Datetime.now(),
                "attribution_locked_by": self.env.user.id,
            })
        return True

    def action_unlock_attribution(self):
        if not self.env.user.has_group("sales_team.group_sale_manager"):
            raise ValidationError(_("Only Sales Managers can unlock attribution."))

        for order in self:
            if not order.attribution_locked:
                continue
            order.with_context(bypass_attribution_lock=True).write({
                "attribution_locked": False,
                "attribution_locked_at": False,
                "attribution_locked_by": False,
            })
        return True

    # ----------------------------
    # UI onchange (NO write recursion)
    # ----------------------------
    @api.onchange("partner_code_input")
    def _onchange_partner_code_input(self):
        for order in self:
            code = (order.partner_code_input or "").strip()
            if not code:
                order.attributed_partner_id = False
                return

            partner = order._find_partner_by_code(code)
            if partner:
                order.attributed_partner_id = partner
            else:
                order.attributed_partner_id = False
                return {
                    "warning": {
                        "title": _("Partner not found / not approved"),
                        "message": _("No approved partner found for code: %s") % code,
                    }
                }

    @api.onchange("attributed_partner_id")
    def _onchange_attributed_partner_id_set_code(self):
        for order in self:
            order.partner_code_input = order.attributed_partner_id.partner_code if order.attributed_partner_id else False

    # ----------------------------
    # Create / Write
    # ----------------------------
    @api.model_create_multi
    def create(self, vals_list):
        new_vals_list = []
        for vals in vals_list:
            vals = dict(vals)

            # Auto-capture referral code ONLY if user didn't set anything
            auto_from_cookie = False
            if not vals.get("partner_code_input") and not vals.get("attributed_partner_id"):
                ref_code = (self._get_referral_code_from_http() or "").strip()
                if ref_code:
                    vals["partner_code_input"] = ref_code
                    auto_from_cookie = True

            # Sync from code -> partner
            self._sync_attributed_partner_from_code(vals)

            # If code was auto from cookie but invalid/unapproved, clear it (prevents garbage codes on SO)
            if auto_from_cookie and vals.get("partner_code_input") and not vals.get("attributed_partner_id"):
                vals["partner_code_input"] = False

            # Sync from partner -> code (if partner was set directly)
            self._sync_code_from_attributed_partner(vals)

            if vals.get("attribution_locked"):
                vals.setdefault("attribution_locked_at", fields.Datetime.now())
                vals.setdefault("attribution_locked_by", self.env.user.id)

            new_vals_list.append(vals)

        return super().create(new_vals_list)

    def write(self, vals):
        if self.env.context.get("skip_partner_code_sync"):
            return super().write(vals)

        # handle multi-write safely without recursion
        if len(self) > 1 and any(k in vals for k in ("partner_code_input", "attributed_partner_id", "attribution_locked")):
            ok = True
            for order in self:
                ok = ok and bool(order.with_context(skip_partner_code_sync=True).write(vals))
            return ok

        vals = dict(vals)

        if self.attribution_locked and not self.env.context.get("bypass_attribution_lock"):
            blocked = {"partner_code_input", "attributed_partner_id"}
            if blocked.intersection(vals.keys()):
                raise ValidationError(_("Attribution is locked. Unlock first to change Partner Code / Attributed Partner."))

        if "attribution_locked" in vals:
            if vals.get("attribution_locked"):
                vals.setdefault("attribution_locked_at", fields.Datetime.now())
                vals.setdefault("attribution_locked_by", self.env.user.id)
            else:
                vals.setdefault("attribution_locked_at", False)
                vals.setdefault("attribution_locked_by", False)

        self._sync_attributed_partner_from_code(vals)
        self._sync_code_from_attributed_partner(vals)

        return super(SaleOrder, self.with_context(skip_partner_code_sync=True)).write(vals)

    # ----------------------------
    # Propagate to Invoice (SO -> Invoice)
    # ----------------------------
    def _prepare_invoice(self):
        vals = super()._prepare_invoice()

        if self.attributed_partner_id:
            locked_by = self.attribution_locked_by.id if self.attribution_locked_by else self.env.user.id

            vals.update({
                "attributed_partner_id": self.attributed_partner_id.id,
                "attribution_locked": True,
                "attribution_locked_at": self.attribution_locked_at or fields.Datetime.now(),
                "attribution_locked_by": locked_by,
            })

        return vals