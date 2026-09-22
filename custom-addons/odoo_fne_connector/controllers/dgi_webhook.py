from odoo import http
from odoo.http import request


class FneWebhookController(http.Controller):
    @http.route('/fne/webhook', type='json', auth='none', methods=['POST'], csrf=False)
    def fne_webhook(self, **post):
        # Ex: POST body: { 'invoice_number': ..., 'status': 'accepted' }
        ref = post.get('invoice_number')
        # ici on suppose que la DGI peut envoyer un header X-Company-ApiKey
        header_key = request.httprequest.headers.get('X-Company-Apikey')
        company = request.env['fne.config'].sudo().search([('fne_api_key', '=', header_key)], limit=1).company_id
        if not company:
            return {'error': 'company not found'}
        inv = request.env['fne.invoice'].sudo().search([('name', '=', ref), ('company_id', '=', company.id)], limit=1)
        if inv:
            inv.sudo().write({'status': post.get('status'), 'response': str(post)})
        return {'ok': True}
