# -*- coding: utf-8 -*-
{
    'name': 'APS Connector',
    'version': '19.0.1.11.0',
    'category': 'Manufacturing',
    'summary': 'Connect Odoo to APS 4 Odoo for Advanced Planning & Scheduling',
    'author': 'Avalah',
    'website': 'https://aps4odoo.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mrp',
        'sale_mrp',
        'stock',
        'purchase',
        'resource',
    ],
    'data': [
        'security/aps_security.xml',
        'security/ir.model.access.csv',
        'views/aps_sync_config_views.xml',
        'views/menu_views.xml',
        'data/aps_sync_config_data.xml',
    ],
    'images': ['static/description/banner.png'],
}
