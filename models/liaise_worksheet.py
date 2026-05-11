from odoo import fields, models


class LiaiseWorksheet(models.Model):
    _inherit = 'x_project_task_worksheet_template_3'

    reza_rep_ids = fields.Many2many(
        'res.users',
        related='x_project_task_id.user_ids',
        string='Rep',
    )
