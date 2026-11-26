from collections import defaultdict
from math import isfinite, isclose
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.addons.acme_project_core.models.boq import _IMPORT_TOKEN


def _tender_bill_number(line):
    # Same fallback as Blen's uploaded addon. Avoid importing a private
    # helper across different deployed versions of that addon.
    if line.bill_no:
        return line.bill_no
    match = re.match(r"^\s*(\d+)(?:\.|$)", line.item_ref or "")
    return match.group(1) if match else False


def _effective_rate(quantity, amount):
    if not isfinite(quantity) or not isfinite(amount) or quantity < 0 or amount < 0:
        raise ValueError('Invalid quantity or amount')
    if not quantity:
        if amount:
            raise ValueError('Nonzero amount with zero quantity')
        return 0.0
    rate = amount / quantity
    if not isfinite(rate):
        raise ValueError('Nonfinite effective rate')
    return rate


class TenderSubmission(models.Model):
    _inherit = 'tender.submission.request'
    _rec_names_search = ['name', 'reference']

    acme_submission_company_id = fields.Many2one(
        'res.company', string='Company for BOQ Loading', copy=False,
        groups='acme_project_core.group_acme_edm,base.group_system',
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
        groups='acme_project_core.group_acme_edm,base.group_system',
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
        if linked:
            linked.check_access('read')
        if len(linked) > 1 or (linked and self.acme_opportunity_id and linked != self.acme_opportunity_id):
            raise UserError(_('The submission has a conflicting or ambiguous originating opportunity.'))
        return linked

    def _acme_submission_contract_order(self):
        self.ensure_one()
        # Existing ACME contract links take precedence; the native Sales CRM
        # source order is discovered even before ACME registration.
        native = self.x_sales_order_id if 'x_sales_order_id' in self._fields else self.env['sale.order']
        candidates = self.acme_order_id | self.acme_registration_quote_id | native
        if len(candidates) > 1:
            raise UserError(_('The project has conflicting contract order references.'))
        return candidates

    def _acme_expected_submission(self, order):
        self.ensure_one()
        if order:
            order.check_access('read')
            if 'x_project_tender_request_id' in order._fields and order.x_project_tender_request_id:
                return order.x_project_tender_request_id
        opportunity = self.acme_opportunity_id
        if 'x_project_opportunity_id' in self._fields:
            opportunity = opportunity or self.x_project_opportunity_id
        if opportunity:
            opportunity.check_access('read')
            if 'x_project_tender_request_id' in opportunity._fields:
                return opportunity.x_project_tender_request_id
        return self.env['tender.submission.request']

    def _acme_submission_values(self, source, order=None):
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
        sale_lines = order.order_line.filtered(lambda line: not line.display_type and not line.is_downpayment) if order else self.env['sale.order.line']
        if sale_lines:
            sale_lines.check_access('read')
        remaining = sale_lines
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
            if not bill:
                raise UserError(_('Create or synchronize Summary of Bills for bill %s before importing.', number))
            try:
                rate = _effective_rate(line.quantity, line.amount)
            except ValueError:
                raise UserError(_('Submission item %s has a nonzero amount but no billable quantity, or an invalid effective rate.', line.item_ref or line.description))
            # The signed net line amount includes discounts and extra costs.
            # An imported gross Excel amount is acceptable only when it matches
            # the identifiable pre-adjustment amount; arbitrary differences fail.
            additional = line.additional_cost if 'additional_cost' in line._fields else 0.0
            discount = line.discount if 'discount' in line._fields else 0.0
            base = line.quantity * line.unit_rate
            if (not isfinite(additional) or not isfinite(discount) or not 0 <= discount <= 100
                    or currency.compare_amounts((base + additional) * (1 - discount / 100), line.amount)):
                raise UserError(_('The approved net amount on submission item %s is inconsistent with its pricing.', line.item_ref or line.description))
            if line.source_amount and (not isfinite(line.source_amount) or all(
                currency.compare_amounts(line.source_amount, candidate)
                for candidate in (line.amount, base, base + additional)
            )):
                raise UserError(_('The uploaded amount for %s does not reconcile with the original or adjusted pricing.', line.item_ref or line.description))
            sale_line = self.env['sale.order.line']
            if order:
                # Match by contract value/quantity and identity, never position.
                eligible = remaining.filtered(lambda row: (
                    isclose(row.product_uom_qty, line.quantity, rel_tol=0, abs_tol=1e-9)
                    and not currency.compare_amounts(row.price_subtotal, line.amount)
                    and (not line.product_id or row.product_id == line.product_id)
                    and bool(row.product_id)
                ))
                def same_description(row):
                    description = (line.description or '').strip()
                    name = (row.name or '').strip()
                    return bool(description and (name == description or name.startswith(description + '\n')))
                def same_reference(row):
                    if not line.item_ref:
                        return False
                    ref = str(line.item_ref).strip()
                    return bool(('x_item_ref' in row._fields and (row.x_item_ref or '').strip() == ref)
                                or (_('Item: %s') % ref) in (row.name or '').splitlines()
                                or ('Item: %s' % ref) in (row.name or '').splitlines())
                by_reference = eligible.filtered(same_reference)
                matches = by_reference or eligible.filtered(same_description)
                if len(matches) != 1:
                    raise UserError(_('Cannot match submission item %s to exactly one confirmed contract line with the same quantity, product and net amount. Correct the source links/prices before importing.', line.item_ref or line.description))
                sale_line = matches
                remaining -= sale_line
            grouped[number] += line.amount
            values.append({
                'project_id': self.id,
                'acme_submission_line_id': line.id,
                'source_line_id': sale_line.id,
                'sequence': line.sequence,
                'reference': line.item_ref,
                'name': line.description,
                'bill_no': number,
                'category': bill.category_id.name or bill.description if bill else False,
                'product_name': (sale_line.product_id or line.product_id).display_name,
                'unit': line.unit,
                'quantity': line.quantity,
                'unit_rate': rate,
                'selling_rate': rate,
            })
        if remaining:
            raise UserError(_('The contract contains item lines not matched to the approved submission. Reconcile the full BOQ before importing.'))
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
        if not (self.env.user.has_group('acme_project_core.group_acme_edm')
                or self.env.user.has_group('base.group_system')):
            raise AccessError(_('Only an EDM or Settings Administrator can verify and load a BOQ submission.'))
        self.check_access('write')
        if not self.env.user.has_group('project.group_project_manager'):
            raise AccessError(_('Project Administrator access is also required to load and finalize the contract BOQ.'))
        order = self._acme_submission_contract_order()
        source = self.acme_boq_submission_id or self._acme_expected_submission(order)
        if not source:
            raise UserError(_('Select a BOQ Submission Reference first.'))
        source.check_access('read')
        opportunity = self._acme_submission_opportunity(source)
        # Consistent lead -> project lock order for Sales CRM integration.
        if opportunity:
            self.env.cr.execute('UPDATE crm_lead SET id = id WHERE id = %s', [opportunity.id])
        self._acme_lock_boq()
        if self.acme_boq_submission_loaded:
            return True
        order = self._acme_submission_contract_order()
        expected = self._acme_expected_submission(order)
        current = self.acme_boq_submission_id or expected
        if current != source or (expected and expected != source):
            raise UserError(_('Select the exact Tender Submission used by the confirmed contract. The selected source has changed or conflicts with the agreement.'))
        if self._acme_submission_opportunity(source) != opportunity:
            raise UserError(_('The submission opportunity changed. Reopen the project and retry.'))
        self._acme_check_registration_unlocked()
        if self.acme_boq_initialized or self.acme_boq_ids or self.acme_boq_history_ids:
            raise UserError(_('This project already has a BOQ baseline or history. Use the amendment workflow.'))
        self.env.cr.execute('UPDATE tender_submission_request SET id = id WHERE id = %s', [source.id])
        source.invalidate_recordset()
        if source.state != 'won':
            raise UserError(_('Only a Won Tender Submission Request can be loaded.'))
        source.partner_id.check_access('read')
        native_opportunity = self.x_project_opportunity_id if 'x_project_opportunity_id' in self._fields else self.env['crm.lead']
        if native_opportunity and native_opportunity != opportunity:
            raise UserError(_('The submission must belong to this Sales CRM opportunity.'))
        company = self._acme_submission_company(source, opportunity)
        # Narrow read-only duplicate discovery across record rules.
        if self.env['project.project'].sudo().with_context(active_test=False).search_count([
            ('id', '!=', self.id), ('acme_boq_submission_id', '=', source.id), ('acme_boq_submission_loaded', '=', True),
        ]):
            raise UserError(_('This submission has already been loaded into another project. Reuse the existing project.'))
        if self.partner_id and self.partner_id.commercial_partner_id != source.partner_id.commercial_partner_id:
            raise UserError(_('The submission customer differs from the existing project customer.'))
        if opportunity and opportunity._acme_uses_sales_project_flow():
            if not order or opportunity.sudo()._acme_existing_project(order.sudo()).ids != self.ids:
                raise UserError(_('Open the single Operations Project created by the confirmed Project Sales order. Do not import this submission into a second manual project.'))
            opportunity._acme_adopt_sales_project(self, order)
        if (opportunity.partner_id and opportunity.partner_id.commercial_partner_id != source.partner_id.commercial_partner_id):
            raise UserError(_('The submission and its originating opportunity must identify the same customer.'))
        if order:
            order.check_access('read')
            if (order.state != 'sale' or order.company_id != company or order.currency_id != source.currency_id
                    or order.partner_id.commercial_partner_id != source.partner_id.commercial_partner_id
                    or source.currency_id.compare_amounts(order.amount_untaxed, source.amount_untaxed)
                    or source.currency_id.compare_amounts(order.amount_total, source.amount_total)
                    or (opportunity and order.opportunity_id != opportunity)):
                raise UserError(_('The submission does not reconcile with the existing contract order or quotation.'))
        if order:
            # Existing ordinary customer billing cannot be treated as zero IPC history.
            # Advance/downpayment lines are not measured BOQ work.
            # The caller has already been checked against the project, order,
            # company and source. Only discover whether billing exists, without
            # requiring unrelated Accounting privileges or revealing invoices.
            billed = order.order_line.sudo().filtered(lambda line: not line.display_type and not line.is_downpayment).invoice_lines.filtered(
                lambda line: line.move_id.state != 'cancel' and line.move_id.move_type in ('out_invoice', 'out_refund')
            )
            if billed:
                raise UserError(_('The original contract already has customer billing on its BOQ items. Finance must reconcile it before establishing a new IPC baseline.'))
        if not order and (self.acme_contract_amount or self.acme_contract_total):
            if (self.acme_contract_currency_id != source.currency_id
                    or (self.acme_contract_amount and source.currency_id.compare_amounts(self.acme_contract_amount, source.amount_untaxed))
                    or (self.acme_contract_total and source.currency_id.compare_amounts(self.acme_contract_total, source.amount_total))):
                raise UserError(_('The approved submission does not match the existing project contract values.'))
        values = self._acme_submission_values(source, order)
        importer = self.with_context(acme_import_token=_IMPORT_TOKEN)
        importer.write({
            'company_id': company.id,
            'partner_id': source.partner_id.id,
            'acme_boq_submission_id': source.id,
            'acme_contract_reference': self.acme_contract_reference or source.reference,
            'acme_contract_currency_id': source.currency_id.id,
            'acme_contract_amount': source.amount_untaxed,
            'acme_contract_total': source.amount_total,
            'acme_boq_vat_rate': source.vat_rate,
            'acme_submission_opportunity_id': opportunity.id,
        })
        created = importer.env['acme.project.boq'].create(values)
        # Monetary fields may round rates when stored. Never finalize a baseline
        # that cannot represent the source line/bill totals at actual precision.
        currency = source.currency_id
        grouped = defaultdict(float)
        for line in created:
            expected_amount = line.acme_submission_line_id.amount
            if (currency.compare_amounts(line.amount, expected_amount)
                    or currency.compare_amounts(line.quantity * line.selling_rate, expected_amount)):
                raise UserError(_('The effective rate for %s cannot reproduce the signed amount at project currency precision. Reconcile the approved pricing before importing.', line.reference or line.name))
            grouped[line.bill_no] += line.amount
        if (any(currency.compare_amounts(bill.amount, grouped[bill.bill_no]) for bill in source.bill_ids)
                or currency.compare_amounts(sum(grouped.values()), source.amount_untaxed)):
            raise UserError(_('Stored BOQ and Summary totals do not match the approved submission. Nothing was finalized.'))
        importer.write({'acme_boq_submission_loaded': True, 'acme_boq_initialized': True})
        self.message_post(body=_('BOQ and Summary loaded from Won Tender Submission %s.', source.reference))
        return True
