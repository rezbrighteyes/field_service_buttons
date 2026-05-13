/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { StateSelectionField } from "@web/views/fields/state_selection/state_selection_field";

const ALLOWED_STATES = ["01_in_progress", "1_canceled", "1_done"];

patch(StateSelectionField.prototype, {
    get options() {
        const items = super.options;
        if (this.props.record?.resModel === "project.task" &&
            this.props.record?.data?.is_fsm) {
            return items.filter(([state]) => ALLOWED_STATES.includes(state));
        }
        return items;
    },
});
