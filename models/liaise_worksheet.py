# -*- coding: utf-8 -*-
import base64
from io import BytesIO
from odoo import api, models
import logging

_logger = logging.getLogger(__name__)

MAX_WIDTH = 1200
MAX_HEIGHT = 900
JPEG_QUALITY = 75


class LiaiseWorksheet(models.Model):
    _inherit = 'x_project_task_worksheet_template_3'

    def _compress_image(self, image_data):
        """Resize and compress image to reduce database storage."""
        if not image_data:
            return image_data
        try:
            from PIL import Image as PILImage
            img_bytes = base64.b64decode(image_data)
            img = PILImage.open(BytesIO(img_bytes))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.thumbnail((MAX_WIDTH, MAX_HEIGHT), PILImage.LANCZOS)
            buffer = BytesIO()
            img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
            return base64.b64encode(buffer.getvalue()).decode()
        except Exception as e:
            _logger.warning('Image compression failed: %s', e)
            return image_data

    @api.model_create_multi
    def create(self, vals_list):
        photo_fields = [
            'x_display_photo', 'x_display_photo_2',
            'x_display_photo_3', 'x_display_photo_4',
        ]
        for vals in vals_list:
            for field in photo_fields:
                if vals.get(field):
                    vals[field] = self._compress_image(vals[field])
        return super().create(vals_list)

    def write(self, vals):
        photo_fields = [
            'x_display_photo', 'x_display_photo_2',
            'x_display_photo_3', 'x_display_photo_4',
        ]
        for field in photo_fields:
            if vals.get(field):
                vals[field] = self._compress_image(vals[field])
        return super().write(vals)
