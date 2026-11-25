from collections import defaultdict
from math import isfinite

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.addons.acme_project_core.models.boq import _IMPORT_TOKEN
from odoo.addons.tender_submission_request.models.tender_submission import _tender_bill_number


class TenderSubmission(models.Model):
    _inherit = 'tender.submission.request'
    _rec_names_search = ['name', 'reference']

    acme_submission_company_id = fields.Many2one(
        'res.company', string='Company for BOQ Loading', copy=False,
        groups='acme_project_core.group_acme_edm',
        help='Set this for standalone submissions with no originating CRM opportunity.',
    )

    @api.constrains('acme_submission_company_id')
    def _check_acme_submission_company(self):
        if self.acme_submission_company_id - self.env.companies:
            raise AccessError(_('Choose a submission company from your selected companies.'))

    def write(self, vals):
        if 'acme_submission_company_id' in vals:
            self.check_access('write')
            self.env.cr.execute(
                'SELECT id FROM tender_submission_request WHERE id IN %s ORDER BY id FOR UPDATE',
                [tuple(self.ids) or (0,)],
            )
            self.invalidate_recordset(['acme_submission_company_id'])
            if self.acme_submission_company_id - self.env['res.company'].browse(vals['acme_submission_company_id']):
                raise UserError(_('The assigned submission company cannot be changed.'))
        return super().write(vals)


class ProjectBoq(models.Model):
    _inherit = 'acme.project.boq'

    acme_submission_line_id = fields.Many2one(
        'tender.submission.line', string='Source Submission Line',
        readonly=True, copy=False, ondelete='restrict',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('acme_import_token') is not _IMPORT_TOKEN and any(
            vals.get('acme_submission_line_id') for vals in vals_list
        ):
            raise AccessError(_('Use Verify / Load BOQ to set the source submission line.'))
        return super().create(vals_list)


class Project(models.Model):
    _inherit = 'project.project'

    acme_boq_submission_id = fields.Many2one(
        'tender.submission.request', string='BOQ Submission Reference',
        copy=False, ondelete='restrict',
        groups='acme_project_core.group_acme_edm',
    )
    acme_boq_submission_loaded = fields.Boolean(readonly=True, copy=False)
    acme_submission_opportunity_id = fields.Many2one(
        'crm.lead', string='Submission Opportunity', check_company=True,
        readonly=True, copy=False, ondelete='restrict',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('acme_boq_submission_loaded') or vals.get('acme_submission_opportunity_id')
               for vals in vals_list):
            raise AccessError(_('Use Verify / Load BOQ to record submission provenance.'))
        return super().create(vals_list)

    def write(self, vals):
        if {'acme_boq_submission_loaded', 'acme_submission_opportunity_id'} & vals.keys():
            if self.env.context.get('acme_import_token') is not _IMPORT_TOKEN:
                raise AccessError(_('Use Verify / Load BOQ to record submission provenance.'))
        if 'acme_boq_submission_id' in vals:
            for project in self.sorted('id'):
                project._acme_lock_boq()
                project._acme_check_registration_unlocked()
                if project.acme_boq_initialized or project.acme_boq_ids or project.acme_boq_history_ids:
                    raise UserError(_('A project with an existing BOQ cannot change its submission reference.'))
        return super().write(vals)

    def _acme_submission_opportunity(self, source):
        leads = self.env['crm.lead']
        linked = source['x_project_opportunity_id'] if 'x_project_opportunity_id' in source._fields else leads
        if not linked and 'x_project_tender_request_id' in leads._fields:
            linked = leads.search([('x_project_tender_request_id', '=', source.id)], limit=2)
        linked.check_access('read')
        if len(linked) > 1 or (linked and self.acme_opportunity_id and linked != self.acme_opportunity_id):
            raise UserError(_('The submission has a conflicting or ambiguous originating opportunity.'))
        return linked

    def _acme_submission_values(self, source):
        self.ensure_one()
        lines = source.line_ids
        bills = source.bill_ids
        lines.check_access('read')
        bills.check_access('read')
        currency = source.currency_id
        if not lines or not currency or not source.reference or not source.partner_id:
            raise UserError(_('The won submission requires a reference, customer, currency and priced BOQ.'))
        if not isfinite(source.vat_rate) or source.vat_rate < 0:
            raise UserError(_('The submission has an invalid VAT rate.'))
        grouped = defaultdict(float)
        bill_by_number = {bill.bill_no: bill for bill in bills}
        values = []
        for line in lines:
            number = _tender_bill_number(line)
            bill = bill_by_number.get(number)
            if not number or not line.description or not line.unit or any(
                not isfinite(value) or value < 0 for value in (line.quantity, line.unit_rate, line.amount)
            ):
                raise UserError(_('Submission item %s needs a bill number, description, unit and valid quantities and rates.',
                                  line.item_ref or line.display_name))
            if line.source_amount and currency.compare_amounts(line.source_amount, line.amount):
                raise UserError(_('The uploaded amount for %s differs from quantity × selling rate. Reconcile the submission first.',
                                  line.item_ref or line.description))
            grouped[number] += line.amount
            values.append({
                'project_id': self.id,
                'acme_submission_line_id': line.id,
                'sequence': line.sequence,
                'reference': line.item_ref,
                'name': line.description,
                'bill_no': number,
                'category': bill.category_id.name or bill.description if bill else False,
                'product_name': line.product_id.display_name,
                'unit': line.unit,
                'quantity': line.quantity,
                'unit_rate': line.unit_rate,
                'selling_rate': line.unit_rate,
            })
        untaxed = sum(grouped.values())
        if (len(bill_by_number) != len(bills)
                or any(currency.compare_amounts(bill.amount, grouped.get(bill.bill_no, 0)) for bill in bills)
                or currency.compare_amounts(untaxed, source.amount_untaxed)
                or currency.compare_amounts(untaxed * (1 + source.vat_rate / 100), source.amount_total)
                or currency.compare_amounts(untaxed, 0) <= 0):
            raise UserError(_('Submission BOQ, Summary of Bills and contract totals must agree before import.'))
        return values

    def _acme_submission_company(self, source, opportunity):
        self.ensure_one()
        companies = self.env['res.company']
        source_company = source.company_id if 'company_id' in source._fields else companies
        configured_company = source.acme_submission_company_id
        candidates = source_company | opportunity.company_id | configured_company
        if len(candidates) > 1:
            raise UserError(_(
                'The submission company conflicts with its originating opportunity or Company for BOQ Loading.'
            ))
        company = source_company or opportunity.company_id or configured_company
        if not company:
            raise UserError(_(
                'No company can be resolved for this submission. For a standalone submission, set '
                'Company for BOQ Loading on the Tender Submission Request.'
            ))
        if company not in self.env.companies:
            raise AccessError(_(
                'You do not have access to the submission company %s. Add it to your allowed companies '
                'before loading the BOQ.', company.display_name,
            ))
        if self.company_id and self.company_id != company:
            raise UserError(_(
                'The project company %(project_company)s differs from the submission company '
                '%(submission_company)s.',
                project_company=self.company_id.display_name,
                submission_company=company.display_name,
            ))
        if source.partner_id.company_id and source.partner_id.company_id != company:
            raise UserError(_('The submission customer belongs to a different company.'))
        return company

    def action_acme_load_boq_submission(self):
        self.ensure_one()
        if not self.env.user.has_group('acme_project_core.group_acme_edm'):
            raise AccessError(_('Only an EDM can verify and load a BOQ submission.'))
        self._acme_lock_boq()
        source = self.acme_boq_submission_id
        if not source:
            raise UserError(_('Select a BOQ Submission Reference first.'))
        source.check_access('read')
        if self.acme_boq_submission_loaded:
            return True
        self._acme_check_registration_unlocked()
        if self.acme_boq_initialized or self.acme_boq_ids or self.acme_boq_history_ids:
            raise UserError(_('This project already has a BOQ baseline or history. Use the amendment workflow.'))
        if source.state != 'won':
            raise UserError(_('Only a Won Tender Submission Request can be loaded.'))
        source.partner_id.check_access('read')
        opportunity = self._acme_submission_opportunity(source)
        company = self._acme_submission_company(source, opportunity)
        if (opportunity.partner_id and opportunity.partner_id.commercial_partner_id != source.partner_id.commercial_partner_id):
            raise UserError(_('The submission and its originating opportunity must identify the same customer.'))
        for order in self.acme_order_id | self.acme_registration_quote_id:
            order.check_access('read')
            if (order.company_id != company or order.currency_id != source.currency_id
                    or order.partner_id.commercial_partner_id != source.partner_id.commercial_partner_id
                    or source.currency_id.compare_amounts(order.amount_untaxed, source.amount_untaxed)
                    or source.currency_id.compare_amounts(order.amount_total, source.amount_total)
                    or (opportunity and order.opportunity_id != opportunity)):
                raise UserError(_('The submission does not reconcile with the existing contract order or quotation.'))
        values = self._acme_submission_values(source)
        importer = self.with_context(acme_import_token=_IMPORT_TOKEN)
        importer.write({
            'company_id': company.id,
            'partner_id': source.partner_id.id,
            'acme_contract_reference': source.reference,
            'acme_contract_currency_id': source.currency_id.id,
            'acme_contract_amount': source.amount_untaxed,
            'acme_contract_total': source.amount_total,
            'acme_boq_vat_rate': source.vat_rate,
            'acme_submission_opportunity_id': opportunity.id,
        })
        importer.env['acme.project.boq'].create(values)
        importer.write({'acme_boq_submission_loaded': True, 'acme_boq_initialized': True})
        self.message_post(body=_('BOQ and Summary loaded from Won Tender Submission %s.', source.reference))
        return True
