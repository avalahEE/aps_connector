# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError
import hashlib
import hmac
import secrets


class ApsSyncConfig(models.Model):
    """APS Synchronization Configuration"""
    _name = 'aps.sync.config'
    _description = 'APS Sync Configuration'
    _rec_name = 'name'

    name = fields.Char(required=True, default='Default')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
    )

    # API key stored as SHA-256 hash for security
    api_key_hash = fields.Char(
        string='API Key Hash',
        help='SHA-256 hash of the API key (internal)',
        readonly=True,
        copy=False,
    )
    # For UI: shows if key is set, allows setting new key
    api_key_display = fields.Char(
        string='API Key',
        compute='_compute_api_key_display',
        inverse='_inverse_api_key_display',
        help='API key for authenticating APS requests',
    )
    # Version info (auto-detected)
    odoo_version = fields.Char(
        string='Odoo version',
        compute='_compute_odoo_version',
        store=False,
    )

    @api.depends_context('uid')
    def _compute_odoo_version(self):
        """Detect Odoo version"""
        import odoo.release
        for record in self:
            record.odoo_version = str(odoo.release.version_info[0])

    def _compute_api_key_display(self):
        """Show masked indicator if API key is set"""
        for record in self:
            record.api_key_display = '********' if record.api_key_hash else ''

    def _inverse_api_key_display(self):
        """Hash and store the API key when set via UI"""
        for record in self:
            if record.api_key_display and record.api_key_display != '********':
                record.set_api_key(record.api_key_display)

    def set_api_key(self, api_key):
        """Set API key by storing its SHA-256 hash"""
        self.ensure_one()
        if not api_key:
            raise ValidationError(self.env._('API key cannot be empty'))
        # Use SHA-256 for hashing
        key_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
        self.api_key_hash = key_hash

    def verify_api_key(self, api_key):
        """Verify API key against stored hash using constant-time comparison"""
        self.ensure_one()
        if not api_key or not self.api_key_hash:
            return False
        provided_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(provided_hash, self.api_key_hash)

    @staticmethod
    def generate_api_key():
        """Generate a secure random API key"""
        return secrets.token_urlsafe(32)
