/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { ImageField } from "@web/views/fields/image/image_field";

const WORKSHEET_MODEL_PREFIX = "x_project_task_worksheet_template_";
const PHOTO_FIELDS = new Set([
    "x_display_photo",
    "x_display_photo_2",
    "x_display_photo_3",
    "x_display_photo_4",
]);
const MAX_WIDTH = 1200;
const MAX_HEIGHT = 900;
const JPEG_QUALITY = 0.7;

function loadImage(dataUrl) {
    return new Promise((resolve, reject) => {
        const image = new Image();
        image.addEventListener("load", () => resolve(image), { once: true });
        image.addEventListener("error", reject, { once: true });
        image.src = dataUrl;
    });
}

async function compressPhoto(info) {
    if (!info.type?.startsWith("image/") || ["image/gif", "image/svg+xml"].includes(info.type)) {
        return info;
    }

    const image = await loadImage(`data:${info.type};base64,${info.data}`);
    const scale = Math.min(1, MAX_WIDTH / image.naturalWidth, MAX_HEIGHT / image.naturalHeight);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);

    const context = canvas.getContext("2d");
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    context.drawImage(image, 0, 0, canvas.width, canvas.height);

    const compressedData = canvas.toDataURL("image/jpeg", JPEG_QUALITY).split(",")[1];
    if (compressedData.length >= info.data.length) {
        return info;
    }

    return {
        ...info,
        data: compressedData,
        type: "image/jpeg",
        name: info.name.replace(/\.[^/.]+$/, "") + ".jpg",
        size: Math.ceil((compressedData.length * 3) / 4),
    };
}

patch(ImageField.prototype, {
    async onFileUploaded(info) {
        const isWorksheetPhoto =
            this.props.record.resModel.startsWith(WORKSHEET_MODEL_PREFIX) &&
            PHOTO_FIELDS.has(this.props.name);
        if (isWorksheetPhoto) {
            try {
                info = await compressPhoto(info);
            } catch (error) {
                console.warn("Worksheet photo compression failed; using original image.", error);
            }
        }
        return super.onFileUploaded(info);
    },
});
