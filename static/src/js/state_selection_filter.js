/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { StateSelectionField } from "@web/views/fields/state_selection/state_selection_field";

const ALLOWED_STATES = ["01_in_progress", "1_canceled", "1_done"];

patch(StateSelectionField.prototype, {
    get stateItems() {
        const items = super.stateItems;
        // Only filter on FSM tasks (project.task with fsm_is_fsm_task)
        if (this.props.record?.resModel === "project.task" &&
            this.props.record?.data?.is_fsm) {
            return items.filter(item => ALLOWED_STATES.includes(item[0]));
        }
        return items;
    },
});
