import logging
from datetime import date

from odoo import http, SUPERUSER_ID
from odoo.http import request
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _selection_keys(env, model, field_name):
    """Clés valides d'un champ Selection, lues directement sur le modèle
    plutôt que dupliquées en dur ici, pour n'avoir qu'une seule source de vérité."""
    return [key for key, _label in env[model]._fields[field_name].selection]


class FneExternalInvoiceController(http.Controller):
    @http.route('/fne/external/invoices', type='http', auth='none', methods=['POST'], csrf=False)
    def create_external_invoice(self, **kwargs):
        """Permet à un système externe de créer une facture Odoo et de la certifier
        à la FNE en un seul appel. La clé API (header X-Api-Key) identifie à la fois
        l'appelant et la société cible : chaque société active la FNE indépendamment
        et a sa propre clé (Configuration > Paramètres > Section FNE).

        Payload JSON attendu :
        {
            "external_ref": "BATCH-2026-09-XXXX",   # optionnel, pour l'idempotence
            "move_type": "out_invoice",              # optionnel, défaut out_invoice
            "invoice_date": "2026-09-21",             # optionnel, défaut aujourd'hui
            "payment_method": "virement",             # optionnel, défaut cheque
            "auto_post": true,                        # optionnel, défaut true
            "send_to_fne": true,                      # optionnel, défaut true
            "partner": {
                "external_ref": "CLI-00123",
                "name": "SIVOP SARL",
                "phone": "+225 07 00 00 00 00",
                "email": "contact@sivop.ci",
                "vat": "CI1234567890",
                "template": "b2b",
                "regime_fiscal": "tva"
            },
            "lines": [
                {"product_ref": "DEMO-001", "quantity": 2, "price_unit": 45000, "discount": 0, "description": "..."},
                {"description": "Prestation ponctuelle non cataloguée", "quantity": 1, "price_unit": 250000, "tax_rate": 18}
            ]
        }

        Une ligne peut référencer un produit déjà existant ('product_ref' connu), ou décrire
        une charge ponctuelle qui n'a pas encore de fiche produit : si 'product_ref' est absent
        ou inconnu, un produit de type Service est créé à la volée à partir de 'description' et
        'price_unit' (obligatoires dans ce cas). Fournir 'product_ref' même pour un produit à
        créer permet de le retrouver et de le réutiliser sur les prochains appels.
        """
        # auth='none' ne charge aucun utilisateur : on se lie explicitement au
        # superutilisateur pour que les calculs par défaut (journal...) d'account.move
        # disposent d'un environnement valide.
        env = request.env(user=SUPERUSER_ID)

        header_key = request.httprequest.headers.get('X-Api-Key')
        company = env['res.company'].sudo().search([('fne_external_api_key', '=', header_key)], limit=1) if header_key else env['res.company']
        if not header_key or not company:
            return request.make_json_response({'success': False, 'error': "Clé API invalide ou manquante."}, status=401)
        if not company.fne_enabled:
            return request.make_json_response({'success': False, 'error': "La FNE n'est pas activée pour cette société."}, status=403)

        try:
            payload = request.get_json_data()
        except ValueError:
            return request.make_json_response({'success': False, 'error': "Corps de requête JSON invalide."}, status=400)

        try:
            status, data = self._create_invoice_from_payload(env, company, payload)
            return request.make_json_response(data, status=status)
        except UserError as e:
            env.cr.rollback()
            return request.make_json_response({'success': False, 'error': str(e)}, status=400)
        except Exception as e:
            env.cr.rollback()
            _logger.exception("[FNE][EXTERNAL] Erreur lors de la création de facture externe")
            return request.make_json_response({'success': False, 'error': "Erreur interne : %s" % str(e)}, status=500)

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _create_invoice_from_payload(self, env, company, payload):
        if not isinstance(payload, dict):
            raise UserError("Le corps de la requête doit être un objet JSON.")

        external_ref = (payload.get('external_ref') or '').strip()
        existing = self._find_existing_invoice(env, company, external_ref)
        if existing:
            return 200, self._invoice_response(existing, already_exists=True)

        partner = self._get_or_create_partner(env, company, payload.get('partner') or {})
        invoice_lines = self._build_invoice_lines(env, company, payload.get('lines') or [])
        move_vals = self._build_move_vals(env, company, payload, partner, invoice_lines, external_ref)

        move = env['account.move'].sudo().create(move_vals)
        if payload.get('auto_post', True):
            move.action_post()

        fne_error = self._send_to_fne_if_requested(payload, move)

        env.cr.commit()
        data = self._invoice_response(move)
        if fne_error:
            data['fne_error'] = fne_error
        return 201, data

    def _invoice_response(self, move, already_exists=False):
        return {
            'success': True,
            'already_exists': already_exists,
            'invoice_id': move.id,
            'invoice_name': move.name,
            'state': move.state,
            'fne_certified': bool(move.fne_sent),
            'fne_reference_dgi': move.fne_reference_dgi or None,
            'fne_verification_url': move.fne_verification_url or None,
        }

    # ------------------------------------------------------------------
    # Facture
    # ------------------------------------------------------------------

    def _find_existing_invoice(self, env, company, external_ref):
        if not external_ref:
            return env['account.move']
        return env['account.move'].sudo().search([
            ('fne_external_ref', '=', external_ref),
            ('company_id', '=', company.id),
        ], limit=1)

    def _build_invoice_lines(self, env, company, lines_payload):
        if not lines_payload:
            raise UserError("Aucune ligne de facture fournie ('lines' est vide).")

        return [
            (0, 0, self._build_line_vals(self._resolve_or_create_product(env, company, line), line))
            for line in lines_payload
        ]

    def _resolve_or_create_product(self, env, company, line):
        product_ref = (line.get('product_ref') or '').strip()
        if product_ref:
            product = env['product.product'].sudo().search([
                ('default_code', '=', product_ref),
                ('company_id', 'in', [company.id, False]),
            ], limit=1)
            if product:
                return product
        return self._create_ad_hoc_product(env, company, product_ref, line)

    def _create_ad_hoc_product(self, env, company, product_ref, line):
        """Crée un produit Service à la volée pour une ligne de charge non cataloguée
        (ex : prestation ponctuelle facturée par un système de facturation périodique)."""
        description = line.get('description')
        price_unit = line.get('price_unit')
        if not description or price_unit is None:
            raise UserError(
                "Produit introuvable (product_ref=%s). Pour qu'il soit créé automatiquement, "
                "fournissez 'description' et 'price_unit' sur la ligne." % (product_ref or 'non fourni')
            )

        tax = self._resolve_sale_tax(env, company, line.get('tax_rate'))
        return env['product.product'].sudo().create({
            'name': description,
            'default_code': product_ref or False,
            'type': 'service',
            'sale_ok': True,
            'list_price': float(price_unit),
            'company_id': company.id,
            'taxes_id': [(6, 0, [tax.id])],
        })

    def _resolve_sale_tax(self, env, company, tax_rate):
        rate = float(tax_rate) if tax_rate is not None else 18.0
        tax = env['account.tax'].sudo().search([
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', rate),
            ('company_id', '=', company.id),
        ], limit=1)
        if not tax:
            raise UserError("Aucune taxe de vente à %.2f%% trouvée dans le plan comptable de %s." % (rate, company.name))
        return tax

    def _build_line_vals(self, product, line):
        price_unit = line.get('price_unit')
        return {
            'product_id': product.id,
            'quantity': float(line.get('quantity') or 1),
            'price_unit': float(price_unit) if price_unit is not None else product.list_price,
            'discount': float(line.get('discount') or 0),
            'name': line.get('description') or product.name,
        }

    def _build_move_vals(self, env, company, payload, partner, invoice_lines, external_ref):
        move_type = payload.get('move_type', 'out_invoice')
        if move_type not in ('out_invoice', 'in_invoice'):
            raise UserError("'move_type' doit être 'out_invoice' ou 'in_invoice'.")

        return {
            'move_type': move_type,
            'company_id': company.id,
            'partner_id': partner.id,
            'invoice_date': self._parse_invoice_date(payload.get('invoice_date')),
            'modes_paiement': self._validate_payment_method(env, payload.get('payment_method', 'cheque')),
            'invoice_line_ids': invoice_lines,
            'fne_external_ref': external_ref or False,
        }

    def _validate_payment_method(self, env, payment_method):
        valid = _selection_keys(env, 'account.move', 'modes_paiement')
        if payment_method not in valid:
            raise UserError("'payment_method' invalide. Valeurs possibles : %s" % ', '.join(valid))
        return payment_method

    def _parse_invoice_date(self, date_str):
        if not date_str:
            return date.today()
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            raise UserError("'invoice_date' doit être au format AAAA-MM-JJ.")

    def _send_to_fne_if_requested(self, payload, move):
        if not payload.get('send_to_fne', True) or move.state != 'posted':
            return None
        try:
            move.action_send_to_fne()
        except UserError as e:
            return str(e)
        return None

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------

    def _get_or_create_partner(self, env, company, partner_payload):
        partner = self._find_partner(env, company, partner_payload)
        return partner if partner else self._create_partner(env, company, partner_payload)

    def _find_partner(self, env, company, partner_payload):
        Partner = env['res.partner'].sudo()
        external_ref = (partner_payload.get('external_ref') or '').strip()
        vat = (partner_payload.get('vat') or '').strip()
        email = (partner_payload.get('email') or '').strip()

        identifying_domain = None
        if external_ref:
            identifying_domain = [('ref', '=', external_ref)]
        elif vat:
            identifying_domain = [('vat', '=', vat)]
        elif email:
            identifying_domain = [('email', '=', email)]

        if not identifying_domain:
            return Partner.browse()
        return Partner.search(identifying_domain + [('company_id', 'in', [company.id, False])], limit=1)

    def _create_partner(self, env, company, partner_payload):
        name = partner_payload.get('name')
        phone = partner_payload.get('phone')
        email = (partner_payload.get('email') or '').strip()
        if not name or not phone or not email:
            raise UserError(
                "Client introuvable et données insuffisantes pour le créer : "
                "'partner.name', 'partner.phone' et 'partner.email' sont requis."
            )

        template = self._validate_partner_template(env, partner_payload.get('template', 'b2c'))
        vat = (partner_payload.get('vat') or '').strip()
        if template == 'b2b' and not vat:
            raise UserError("'partner.vat' (NCC) est requis pour créer un client de type B2B.")

        return env['res.partner'].sudo().create({
            'name': name,
            'phone': phone,
            'email': email,
            'ref': (partner_payload.get('external_ref') or '').strip() or False,
            'vat': vat or False,
            'company_id': company.id,
            'is_company': template in ('b2b', 'b2g'),
            'templateFne': template,
            'regimeFiscal': self._validate_fiscal_regime(env, partner_payload.get('regime_fiscal', 'tva')),
        })

    def _validate_partner_template(self, env, template):
        valid = _selection_keys(env, 'res.partner', 'templateFne')
        if template not in valid:
            raise UserError("'partner.template' invalide. Valeurs possibles : %s" % ', '.join(valid))
        return template

    def _validate_fiscal_regime(self, env, regime_fiscal):
        valid = _selection_keys(env, 'res.partner', 'regimeFiscal')
        if regime_fiscal not in valid:
            raise UserError("'partner.regime_fiscal' invalide. Valeurs possibles : %s" % ', '.join(valid))
        return regime_fiscal
