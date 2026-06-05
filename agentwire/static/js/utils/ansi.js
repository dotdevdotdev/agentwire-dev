/**
 * ansi.js — ANSI escape code → HTML conversion.
 *
 * Single source of truth for rendering `tmux capture-pane -e` output in the
 * browser. Used by SessionWindow (monitor mode) and the collage preview tiles.
 */

/**
 * Convert ANSI escape codes to HTML.
 * Supports basic colors, 256-color, and true color (24-bit).
 * @param {string} text - Text with ANSI codes
 * @returns {string} HTML string
 */
export function ansiToHtml(text) {
    // Escape HTML entities first
    let html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    // Basic 16 colors (standard + bright)
    const basicColors = {
        0: '#000', 1: '#cd0000', 2: '#00cd00', 3: '#cdcd00',
        4: '#0000ee', 5: '#cd00cd', 6: '#00cdcd', 7: '#e5e5e5',
        8: '#7f7f7f', 9: '#ff0000', 10: '#00ff00', 11: '#ffff00',
        12: '#5c5cff', 13: '#ff00ff', 14: '#00ffff', 15: '#ffffff'
    };

    // Convert 256-color index to hex
    const color256ToHex = (n) => {
        if (n < 16) return basicColors[n];
        if (n < 232) {
            // 216 color cube: 6x6x6
            n -= 16;
            const r = Math.floor(n / 36) * 51;
            const g = Math.floor((n % 36) / 6) * 51;
            const b = (n % 6) * 51;
            return `rgb(${r},${g},${b})`;
        }
        // Grayscale: 24 shades
        const gray = (n - 232) * 10 + 8;
        return `rgb(${gray},${gray},${gray})`;
    };

    // Track current styles
    let fg = null, bg = null, bold = false, italic = false, underline = false;

    const buildSpan = () => {
        const styles = [];
        if (fg) styles.push(`color:${fg}`);
        if (bg) styles.push(`background:${bg}`);
        if (bold) styles.push('font-weight:bold');
        if (italic) styles.push('font-style:italic');
        if (underline) styles.push('text-decoration:underline');
        return styles.length ? `<span style="${styles.join(';')}">` : '';
    };

    // Process ANSI sequences
    html = html.replace(/\x1b\[([0-9;]*)m/g, (match, codes) => {
        const parts = (codes || '0').split(';').map(Number);
        let i = 0;
        let needsNewSpan = false;

        while (i < parts.length) {
            const code = parts[i];

            if (code === 0) {
                // Reset all
                fg = bg = null;
                bold = italic = underline = false;
                needsNewSpan = true;
            } else if (code === 1) {
                bold = true; needsNewSpan = true;
            } else if (code === 3) {
                italic = true; needsNewSpan = true;
            } else if (code === 4) {
                underline = true; needsNewSpan = true;
            } else if (code === 22) {
                bold = false; needsNewSpan = true;
            } else if (code === 23) {
                italic = false; needsNewSpan = true;
            } else if (code === 24) {
                underline = false; needsNewSpan = true;
            } else if (code >= 30 && code <= 37) {
                fg = basicColors[code - 30]; needsNewSpan = true;
            } else if (code >= 90 && code <= 97) {
                fg = basicColors[code - 90 + 8]; needsNewSpan = true;
            } else if (code === 39) {
                fg = null; needsNewSpan = true;
            } else if (code >= 40 && code <= 47) {
                bg = basicColors[code - 40]; needsNewSpan = true;
            } else if (code >= 100 && code <= 107) {
                bg = basicColors[code - 100 + 8]; needsNewSpan = true;
            } else if (code === 49) {
                bg = null; needsNewSpan = true;
            } else if (code === 38 && parts[i + 1] === 5) {
                // 256-color foreground: 38;5;N
                fg = color256ToHex(parts[i + 2]);
                i += 2; needsNewSpan = true;
            } else if (code === 48 && parts[i + 1] === 5) {
                // 256-color background: 48;5;N
                bg = color256ToHex(parts[i + 2]);
                i += 2; needsNewSpan = true;
            } else if (code === 38 && parts[i + 1] === 2) {
                // True color foreground: 38;2;R;G;B
                fg = `rgb(${parts[i + 2]},${parts[i + 3]},${parts[i + 4]})`;
                i += 4; needsNewSpan = true;
            } else if (code === 48 && parts[i + 1] === 2) {
                // True color background: 48;2;R;G;B
                bg = `rgb(${parts[i + 2]},${parts[i + 3]},${parts[i + 4]})`;
                i += 4; needsNewSpan = true;
            }
            i++;
        }

        // Close previous span and open new one with current styles
        return needsNewSpan ? `</span>${buildSpan()}` : '';
    });

    // Wrap in initial span and close at end
    return `<span>${html}</span>`;
}
