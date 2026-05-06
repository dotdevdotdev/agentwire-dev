/**
 * Sidebar - click-toggle left navigation panel with floating tab handle.
 *
 * A small tab peeks out from the left edge. Click to slide sidebar open,
 * click again to close. No hover behavior — purely intentional.
 */

const PIN_KEY = 'sidebar-pinned';

export const sidebar = {
    el: null,
    tab: null,
    pinBtn: null,

    init() {
        this.el = document.getElementById('sidebar');
        this.tab = document.getElementById('sidebarTab');
        this.pinBtn = document.getElementById('sidebarPin');
        if (!this.el || !this.tab) return;

        // Restore pinned state from localStorage
        if (localStorage.getItem(PIN_KEY) === 'true') {
            this.pin();
        }

        // Tab click → toggle sidebar
        this.tab.addEventListener('click', () => this.toggle());

        // Click outside sidebar → close (unless pinned)
        document.addEventListener('mousedown', (e) => {
            if (!this.isOpen() || this.isPinned()) return;
            if (this.el.contains(e.target) || this.tab.contains(e.target)) return;
            this.close();
        });

        // ESC closes (unless pinned). Cmd/Ctrl + ` toggles.
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && this.isOpen() && !this.isPinned()) {
                this.close();
                return;
            }
            if ((e.metaKey || e.ctrlKey) && (e.key === '`' || e.code === 'Backquote')) {
                e.preventDefault();
                this.toggle();
            }
        });

        // Pin toggle
        this.pinBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.isPinned() ? this.unpin() : this.pin();
        });
    },

    open() {
        if (!this.el) return;
        this.el.classList.add('open');
        document.body.classList.add('sidebar-open');
    },

    close() {
        if (!this.el || this.isPinned()) return;
        this.el.classList.remove('open');
        document.body.classList.remove('sidebar-open');
    },

    toggle() {
        this.isOpen() ? this.close() : this.open();
    },

    isOpen() {
        return !!this.el && this.el.classList.contains('open');
    },

    isPinned() {
        return !!this.el && this.el.classList.contains('pinned');
    },

    pin() {
        if (!this.el) return;
        this.el.classList.add('pinned');
        this.el.classList.add('open');
        document.body.classList.add('sidebar-pinned');
        document.body.classList.add('sidebar-open');
        try { localStorage.setItem(PIN_KEY, 'true'); } catch (e) {}
    },

    unpin() {
        if (!this.el) return;
        this.el.classList.remove('pinned');
        document.body.classList.remove('sidebar-pinned');
        try { localStorage.setItem(PIN_KEY, 'false'); } catch (e) {}
    },

    // --- Accordion sections (Phase 3) ---
    _sections: new Map(),

    addSection(id, sectionObj) {
        const container = document.getElementById('sidebarSections');
        if (!container) return;
        const section = document.createElement('div');
        section.className = 'sidebar-section';
        section.dataset.sectionId = id;

        const header = document.createElement('div');
        header.className = 'sidebar-section-title sidebar-section-toggle';
        const titleSpan = `<span class="sidebar-section-chevron">▸</span> ${sectionObj.title}`;
        const actions = (sectionObj.actions || []).map(a =>
            `<button class="sidebar-section-action" data-action="${a.id}" title="${a.title || a.label}">${a.label}</button>`
        ).join('');
        header.innerHTML = actions
            ? `<span class="sidebar-section-title-text">${titleSpan}</span><span class="sidebar-section-actions">${actions}</span>`
            : titleSpan;
        header.querySelector('.sidebar-section-title-text, .sidebar-section-chevron')
            ?.closest('.sidebar-section-title-text')
            ?.addEventListener('click', () => this.toggleSection(id));
        if (!actions) header.addEventListener('click', () => this.toggleSection(id));
        // Action button clicks
        header.querySelectorAll('.sidebar-section-action').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const entry = this._sections.get(id);
                if (!entry?.el.classList.contains('expanded')) this.expandSection(id);
                sectionObj.onAction?.(btn.dataset.action, entry.body);
            });
        });
        section.appendChild(header);

        const body = document.createElement('div');
        body.className = 'sidebar-section-body';
        body.style.display = 'none';
        section.appendChild(body);

        container.appendChild(section);

        const entry = { id, sectionObj, el: section, header, body, mounted: false, refreshTimer: null };
        this._sections.set(id, entry);
    },

    expandSection(id) {
        const entry = this._sections.get(id);
        if (!entry) return;
        this.open();
        entry.body.style.display = '';
        entry.header.querySelector('.sidebar-section-chevron').textContent = '▾';
        entry.el.classList.add('expanded');
        const s = entry.sectionObj;
        if (!entry.mounted) {
            entry.mounted = true;
            s.mount(entry.body);
        } else if (s.refresh) {
            s.refresh(entry.body);
        }
        if (s.autoRefreshMs && !entry.refreshTimer) {
            entry.refreshTimer = setInterval(() => {
                if (entry.el.classList.contains('expanded') && s.refresh) {
                    s.refresh(entry.body);
                }
            }, s.autoRefreshMs);
        }
        entry.body.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    collapseSection(id) {
        const entry = this._sections.get(id);
        if (!entry) return;
        entry.body.style.display = 'none';
        entry.header.querySelector('.sidebar-section-chevron').textContent = '▸';
        entry.el.classList.remove('expanded');
        if (entry.refreshTimer) {
            clearInterval(entry.refreshTimer);
            entry.refreshTimer = null;
        }
    },

    toggleSection(id) {
        const entry = this._sections.get(id);
        if (!entry) return;
        entry.el.classList.contains('expanded') ? this.collapseSection(id) : this.expandSection(id);
    },
};
