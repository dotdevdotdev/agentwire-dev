import { getAllSessions, renderCard, handleSessionClick, onSessionsChanged } from './sessions-section.js';
import { isService } from '../service-classification.js';

export const servicesSection = {
    title: 'Services',
    _body: null,

    async mount(body) {
        this._body = body;
        onSessionsChanged(() => this._render(body));
        this._render(body);
    },

    async refresh(body) {
        this._render(body);
    },

    _render(body) {
        const services = getAllSessions().filter(s => isService(s.name || ''));
        if (!services.length) {
            body.innerHTML = '<div class="sidebar-empty">No services</div>';
            return;
        }
        body.innerHTML = services.map(s => renderCard(s)).join('');
        body.onclick = (e) => handleSessionClick(e);
    },
};
