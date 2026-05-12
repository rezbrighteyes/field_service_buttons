/** @odoo-module **/

// Lightbox for image fields - click any image in o_field_image to enlarge
document.addEventListener('click', function (ev) {
    const img = ev.target;
    if (img.tagName !== 'IMG') return;
    if (!img.closest('.o_field_image')) return;

    const src = img.src;
    if (!src || src.includes('placeholder')) return;

    ev.preventDefault();
    ev.stopPropagation();

    // Overlay
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:10000;display:flex;align-items:center;justify-content:center;cursor:zoom-out;';

    // Full size image
    const bigImg = document.createElement('img');
    bigImg.src = src;
    bigImg.style.cssText = 'max-width:90vw;max-height:90vh;object-fit:contain;border-radius:4px;box-shadow:0 8px 40px rgba(0,0,0,0.8);';

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.innerHTML = '&times;';
    closeBtn.style.cssText = 'position:absolute;top:16px;right:20px;background:none;border:none;color:#fff;font-size:36px;cursor:pointer;line-height:1;padding:0;opacity:0.8;';

    overlay.appendChild(bigImg);
    overlay.appendChild(closeBtn);

    const close = () => {
        overlay.remove();
        document.removeEventListener('keydown', keyHandler);
    };
    const keyHandler = (e) => { if (e.key === 'Escape') close(); };

    overlay.addEventListener('click', close);
    closeBtn.addEventListener('click', (e) => { e.stopPropagation(); close(); });
    document.addEventListener('keydown', keyHandler);
    document.body.appendChild(overlay);
});