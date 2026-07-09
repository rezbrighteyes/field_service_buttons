# -*- coding: utf-8 -*-
from odoo import fields, models, tools


class FsmRepOperationReport(models.Model):
    _name = "reza.fsm.rep.operation.report"
    _description = "Rep Operations Control Center"
    _auto = False
    _rec_name = "task_id"
    _order = "exception_priority, planned_date desc, task_id desc"

    task_id = fields.Many2one("project.task", string="Task / Visit", readonly=True)
    parent_task_id = fields.Many2one("project.task", string="Parent Run", readonly=True)
    run_task_id = fields.Many2one("project.task", string="Run", readonly=True)
    task_level = fields.Selection(
        [("run", "Run"), ("visit", "Visit / Sub-task")],
        string="Type",
        readonly=True,
    )
    rep_id = fields.Many2one("res.users", string="Rep", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Customer / Store", readonly=True)
    company_id = fields.Many2one("res.company", string="Company", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)
    stage_id = fields.Many2one("project.task.type", string="Stage", readonly=True)
    state = fields.Selection(
        [
            ("01_in_progress", "In Progress"),
            ("02_changes_requested", "Changes Requested"),
            ("03_approved", "Approved"),
            ("04_waiting_normal", "Waiting"),
            ("1_done", "Done"),
            ("1_canceled", "Cancelled"),
        ],
        string="Task State",
        readonly=True,
    )
    status_bucket = fields.Selection(
        [
            ("new", "New"),
            ("planned", "Planned"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        readonly=True,
    )
    planned_date = fields.Date(string="Planned Date", readonly=True)
    planned_date_begin = fields.Datetime(string="Planned Start", readonly=True)
    date_deadline = fields.Datetime(string="Deadline", readonly=True)
    days_overdue = fields.Integer(string="Days Overdue", readonly=True)
    fsm_next_visit_date = fields.Date(string="Next Visit Date", readonly=True)
    has_worksheet = fields.Boolean(string="Worksheet Exists", readonly=True)
    worksheet_record_count = fields.Integer(string="Worksheet Records", readonly=True)
    worksheet_status = fields.Selection(
        [
            ("missing", "Missing"),
            ("exists", "Exists"),
            ("not_required", "Not Required"),
        ],
        string="Worksheet Status",
        readonly=True,
    )
    has_cancellation_reason = fields.Boolean(string="Cancellation Reason", readonly=True)
    has_credit_return = fields.Boolean(string="Credit / Return Exists", readonly=True)
    credit_return_count = fields.Integer(string="Credit / Return Lines", readonly=True)
    credit_note_count = fields.Integer(string="Credit Notes", readonly=True)
    credit_return_quantity = fields.Float(string="Credit / Return Qty", readonly=True)
    credit_return_value = fields.Monetary(
        string="Credit / Return Value",
        currency_field="currency_id",
        readonly=True,
    )
    exception_type = fields.Selection(
        [
            ("missing_worksheet", "Missing Worksheet"),
            ("worksheet_done_task_open", "Worksheet Done, Task Not Done"),
            ("done_without_worksheet", "Done Without Worksheet"),
            ("missing_cancel_reason", "Missing Cancellation Reason"),
            ("overdue", "Overdue"),
            ("stuck_in_progress", "Stuck In Progress"),
            ("missing_next_visit", "Missing Next Visit"),
            ("ok", "OK"),
        ],
        string="Exception",
        readonly=True,
    )
    exception_priority = fields.Integer(string="Exception Priority", readonly=True)
    task_count = fields.Integer(string="Visits / Runs", readonly=True)
    open_count = fields.Integer(string="Open", readonly=True)
    done_count = fields.Integer(string="Done", readonly=True)
    cancelled_count = fields.Integer(string="Cancelled", readonly=True)
    overdue_count = fields.Integer(string="Overdue", readonly=True)
    missing_worksheet_count = fields.Integer(string="Missing Worksheet", readonly=True)
    worksheet_done_task_open_count = fields.Integer(
        string="Worksheet Done, Task Not Done",
        readonly=True,
    )
    credit_return_task_count = fields.Integer(string="Tasks With Credit / Return", readonly=True)
    create_date = fields.Datetime(string="Created On", readonly=True)
    write_date = fields.Datetime(string="Last Updated", readonly=True)

    def _quote_identifier(self, name):
        return '"%s"' % name.replace('"', '""')

    def _worksheet_union_sql(self):
        parts = []
        for model_name in self.env.registry.models:
            if not model_name.startswith("x_project_task_worksheet_template_"):
                continue
            Worksheet = self.env[model_name]
            if "x_project_task_id" not in Worksheet._fields:
                continue
            parts.append(
                """
                SELECT
                    x_project_task_id AS task_id,
                    COUNT(*)::integer AS worksheet_record_count
                FROM {table}
                WHERE x_project_task_id IS NOT NULL
                GROUP BY x_project_task_id
                """.format(table=self._quote_identifier(Worksheet._table))
            )
        if not parts:
            return """
                SELECT
                    NULL::integer AS task_id,
                    0::integer AS worksheet_record_count
                WHERE FALSE
            """
        return "\nUNION ALL\n".join(parts)

    def init(self):
        task_user_field = self.env["project.task"]._fields["user_ids"]
        rel_table = self._quote_identifier(task_user_field.relation)
        task_col = self._quote_identifier(task_user_field.column1)
        user_col = self._quote_identifier(task_user_field.column2)
        worksheet_union_sql = self._worksheet_union_sql()

        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE OR REPLACE VIEW reza_fsm_rep_operation_report AS (
                WITH worksheet_raw AS (
                    {worksheet_union_sql}
                ),
                worksheet AS (
                    SELECT
                        task_id,
                        SUM(worksheet_record_count)::integer AS worksheet_record_count
                    FROM worksheet_raw
                    GROUP BY task_id
                ),
                credit AS (
                    SELECT
                        event.task_id,
                        COUNT(*)::integer AS credit_return_count,
                        COUNT(DISTINCT event.move_id)::integer AS credit_note_count,
                        COALESCE(SUM(event.quantity), 0.0) AS credit_return_quantity,
                        COALESCE(SUM(ABS(line.balance)), 0.0) AS credit_return_value
                    FROM reza_fsm_credit_return_event event
                    LEFT JOIN account_move_line line ON line.id = event.move_line_id
                    WHERE event.task_id IS NOT NULL
                    GROUP BY event.task_id
                ),
                base AS (
                    SELECT
                        task.id,
                        task.id AS task_id,
                        task.parent_id AS parent_task_id,
                        COALESCE(task.parent_id, task.id) AS run_task_id,
                        CASE WHEN task.parent_id IS NULL THEN 'run' ELSE 'visit' END AS task_level,
                        COALESCE(task_user.user_id, parent_user.user_id) AS rep_id,
                        COALESCE(task.partner_id, parent.partner_id) AS partner_id,
                        COALESCE(task.company_id, project.company_id) AS company_id,
                        company.currency_id,
                        task.stage_id,
                        task.state,
                        CASE
                            WHEN task.state = '1_done' THEN 'done'
                            WHEN task.state = '1_canceled' THEN 'cancelled'
                            WHEN task.planned_date_begin IS NOT NULL AND task.planned_date_begin::date > CURRENT_DATE THEN 'planned'
                            WHEN task.state IS NULL THEN 'new'
                            ELSE 'in_progress'
                        END AS status_bucket,
                        COALESCE(task.planned_date_begin::date, task.date_deadline::date) AS planned_date,
                        task.planned_date_begin,
                        task.date_deadline,
                        task.fsm_next_visit_date,
                        task.fsm_cancellation_reason,
                        COALESCE(worksheet.worksheet_record_count, 0)::integer AS worksheet_record_count,
                        COALESCE(credit.credit_return_count, 0)::integer AS credit_return_count,
                        COALESCE(credit.credit_note_count, 0)::integer AS credit_note_count,
                        COALESCE(credit.credit_return_quantity, 0.0) AS credit_return_quantity,
                        COALESCE(credit.credit_return_value, 0.0) AS credit_return_value,
                        task.create_date,
                        task.write_date
                    FROM project_task task
                    JOIN project_project project ON project.id = task.project_id
                    LEFT JOIN project_task parent ON parent.id = task.parent_id
                    LEFT JOIN res_company company ON company.id = COALESCE(task.company_id, project.company_id)
                    LEFT JOIN worksheet ON worksheet.task_id = task.id
                    LEFT JOIN credit ON credit.task_id = task.id
                    LEFT JOIN LATERAL (
                        SELECT rel.{user_col} AS user_id
                        FROM {rel_table} rel
                        WHERE rel.{task_col} = task.id
                        ORDER BY rel.{user_col}
                        LIMIT 1
                    ) task_user ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT rel.{user_col} AS user_id
                        FROM {rel_table} rel
                        WHERE rel.{task_col} = parent.id
                        ORDER BY rel.{user_col}
                        LIMIT 1
                    ) parent_user ON TRUE
                    WHERE
                        project.is_fsm IS TRUE
                        AND task.active IS TRUE
                ),
                enriched AS (
                    SELECT
                        base.*,
                        (base.worksheet_record_count > 0) AS has_worksheet,
                        (base.credit_return_count > 0) AS has_credit_return,
                        CASE
                            WHEN base.task_level = 'run' THEN 'not_required'
                            WHEN base.worksheet_record_count > 0 THEN 'exists'
                            ELSE 'missing'
                        END AS worksheet_status,
                        COALESCE(NULLIF(BTRIM(base.fsm_cancellation_reason), ''), '') != '' AS has_cancellation_reason,
                        CASE
                            WHEN base.status_bucket NOT IN ('done', 'cancelled')
                                AND base.planned_date IS NOT NULL
                                AND base.planned_date < CURRENT_DATE
                                THEN (CURRENT_DATE - base.planned_date)::integer
                            ELSE 0
                        END AS days_overdue
                    FROM base
                ),
                exceptioned AS (
                    SELECT
                        enriched.*,
                        CASE
                            WHEN enriched.task_level = 'visit'
                                AND enriched.status_bucket = 'done'
                                AND NOT enriched.has_worksheet
                                THEN 'done_without_worksheet'
                            WHEN enriched.task_level = 'visit'
                                AND enriched.status_bucket NOT IN ('done', 'cancelled')
                                AND NOT enriched.has_worksheet
                                THEN 'missing_worksheet'
                            WHEN enriched.task_level = 'visit'
                                AND enriched.status_bucket NOT IN ('done', 'cancelled')
                                AND enriched.has_worksheet
                                THEN 'worksheet_done_task_open'
                            WHEN enriched.task_level = 'visit'
                                AND enriched.status_bucket = 'cancelled'
                                AND NOT enriched.has_cancellation_reason
                                THEN 'missing_cancel_reason'
                            WHEN enriched.status_bucket NOT IN ('done', 'cancelled')
                                AND enriched.days_overdue > 0
                                THEN 'overdue'
                            WHEN enriched.status_bucket = 'in_progress'
                                AND enriched.write_date < (NOW() - INTERVAL '2 days')
                                THEN 'stuck_in_progress'
                            WHEN enriched.task_level = 'visit'
                                AND enriched.status_bucket = 'done'
                                AND enriched.fsm_next_visit_date IS NULL
                                THEN 'missing_next_visit'
                            ELSE 'ok'
                        END AS exception_type
                    FROM enriched
                )
                SELECT
                    exceptioned.id,
                    exceptioned.task_id,
                    exceptioned.parent_task_id,
                    exceptioned.run_task_id,
                    exceptioned.task_level,
                    exceptioned.rep_id,
                    exceptioned.partner_id,
                    exceptioned.company_id,
                    exceptioned.currency_id,
                    exceptioned.stage_id,
                    exceptioned.state,
                    exceptioned.status_bucket,
                    exceptioned.planned_date,
                    exceptioned.planned_date_begin,
                    exceptioned.date_deadline,
                    exceptioned.days_overdue,
                    exceptioned.fsm_next_visit_date,
                    exceptioned.has_worksheet,
                    exceptioned.worksheet_record_count,
                    exceptioned.worksheet_status,
                    exceptioned.has_cancellation_reason,
                    exceptioned.has_credit_return,
                    exceptioned.credit_return_count,
                    exceptioned.credit_note_count,
                    exceptioned.credit_return_quantity,
                    exceptioned.credit_return_value,
                    exceptioned.exception_type,
                    CASE exceptioned.exception_type
                        WHEN 'done_without_worksheet' THEN 1
                        WHEN 'missing_worksheet' THEN 2
                        WHEN 'missing_cancel_reason' THEN 3
                        WHEN 'worksheet_done_task_open' THEN 4
                        WHEN 'overdue' THEN 5
                        WHEN 'stuck_in_progress' THEN 6
                        WHEN 'missing_next_visit' THEN 7
                        ELSE 99
                    END AS exception_priority,
                    1::integer AS task_count,
                    CASE WHEN exceptioned.status_bucket NOT IN ('done', 'cancelled') THEN 1 ELSE 0 END::integer AS open_count,
                    CASE WHEN exceptioned.status_bucket = 'done' THEN 1 ELSE 0 END::integer AS done_count,
                    CASE WHEN exceptioned.status_bucket = 'cancelled' THEN 1 ELSE 0 END::integer AS cancelled_count,
                    CASE WHEN exceptioned.days_overdue > 0 THEN 1 ELSE 0 END::integer AS overdue_count,
                    CASE WHEN exceptioned.exception_type IN ('missing_worksheet', 'done_without_worksheet') THEN 1 ELSE 0 END::integer AS missing_worksheet_count,
                    CASE WHEN exceptioned.exception_type = 'worksheet_done_task_open' THEN 1 ELSE 0 END::integer AS worksheet_done_task_open_count,
                    CASE WHEN exceptioned.has_credit_return THEN 1 ELSE 0 END::integer AS credit_return_task_count,
                    exceptioned.create_date,
                    exceptioned.write_date
                FROM exceptioned
            )
            """.format(
                worksheet_union_sql=worksheet_union_sql,
                rel_table=rel_table,
                task_col=task_col,
                user_col=user_col,
            )
        )
