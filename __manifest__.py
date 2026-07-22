# -*- coding: utf-8 -*-
# noop: rebuild trigger
# noop: rebuild trigger 2
{
    'name': 'Reza Field Service Buttons',
    'version': '19.0.1.0.10',
    'summary': 'Adds Order and Credit Note buttons to Field Service sub-tasks',
    'author': 'Reza',
    'category': 'Field Service',
    'depends': [
        'industry_fsm',
        'sale',
        'account',
        'stock',
        'spreadsheet_dashboard',
        'd3_product_edbert',
        'd3_credit_control',
        'reza_intercompany_warehouse',
    ],
    'data': [
        'security/fsm_security.xml',
        'security/ir.model.access.csv',
        'data/credit_return_reason_data.xml',
        'data/fsm_action_defaults_data.xml',
        'data/liaise_worksheet_report_data.xml',
        'views/rep_operation_report_views.xml',
        'data/rep_operation_dashboard_data.xml',
        'views/credit_return_event_views.xml',
        'views/credit_return_wizard_views.xml',
        'views/product_catalog_views.xml',
        'views/account_move_views.xml',
        'views/account_move_report.xml',
        'views/project_task_views.xml',
        'views/assets.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'reza_field_service_buttons/static/src/css/field_service_buttons.css',
            'reza_field_service_buttons/static/src/js/list_delete_confirm.js',
            'reza_field_service_buttons/static/src/js/lightbox_image.js',
            'reza_field_service_buttons/static/src/js/worksheet_image_compression.js',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'OPL-1',
}
