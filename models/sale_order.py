from odoo import _, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _create_invoices(self, *args, **kwargs):
        # Standard original-order invoice generation competes with IPC billing.
        # ACME progress and advance workflows create their own account.move.
        if self.env['project.project'].sudo().with_context(active_test=False).search_count([
            ('acme_order_id', 'in', self.ids),
            ('acme_boq_submission_loaded', '=', True),
        ]):
            raise UserError(_('The original contract is managed by an imported ACME BOQ. Generate measured-work invoices through Request Bill / IPC, not the Sales Order invoice wizard.'))
        return super()._create_invoices(*args, **kwargs)
