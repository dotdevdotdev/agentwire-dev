/**
 * Jargon correction map for browser speech recognition (default STT tier).
 *
 * Chrome's recognizer is confidently wrong on dev vocabulary ("tmux" → "team
 * up") and offers no vocabulary-biasing API, so we post-correct the final
 * transcript with deterministic rewrites. Built-ins below; users extend via
 * `stt.corrections` in config.yaml (served through /api/voice-status) — user
 * entries win on conflict.
 */

export const BUILTIN_CORRECTIONS = {
    'team up': 'tmux',
    't mux': 'tmux',
    'tea mux': 'tmux',
    'pie test': 'pytest',
    'pi test': 'pytest',
    'worker pain': 'worker pane',
    'agent wire': 'agentwire',
    'get status': 'git status',
    'get push': 'git push',
    'get pull': 'git pull',
    'get diff': 'git diff',
    'get commit': 'git commit',
    'get checkout': 'git checkout',
    'dock her': 'docker',
    'cube control': 'kubectl',
    'cube cuddle': 'kubectl',
    'you v': 'uv',
    'u v run': 'uv run',
    're base': 'rebase',
    'cherry pick': 'cherry-pick',
    'pseudo': 'sudo',
    'em c p': 'MCP',
    'view env': 'venv',
    'v env': 'venv',
};

function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Apply jargon corrections to a transcript.
 * @param {string} text - Raw transcript from SpeechRecognition
 * @param {Object} extra - User corrections from stt.corrections (win on conflict)
 * @returns {string} Corrected transcript
 */
export function applyCorrections(text, extra = {}) {
    const map = { ...BUILTIN_CORRECTIONS, ...extra };
    let out = text;
    for (const [from, to] of Object.entries(map)) {
        out = out.replace(new RegExp(`\\b${escapeRe(from)}\\b`, 'gi'), to);
    }
    return out;
}
