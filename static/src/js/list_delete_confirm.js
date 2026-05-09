/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ListRenderer } from "@web/views/list/list_renderer";

patch(ListRenderer.prototype, {
    onDeleteRecord(record) {
        if (confirm("Are you sure you want to remove this line?")) {
            return super.onDeleteRecord(record);
        }
    },
});
