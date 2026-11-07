/**
 * Mount sidebar sections one at a time so a section that throws cannot abort
 * the caller (desktop init) and leave the rest of the page — hotkeys included —
 * unwired (#1300). A failure is reported through `onError`, never swallowed.
 *
 * @param {{addSection: (id: string, section: object) => void}} sidebar
 * @param {Array<[string, object]>} sections  [id, sectionObj] pairs, in order
 * @param {(id: string, err: unknown) => void} onError
 * @returns {string[]} ids that failed to mount
 */
export function addSectionsIsolated(sidebar, sections, onError) {
    const failed = [];
    for (const [id, section] of sections) {
        try {
            sidebar.addSection(id, section);
        } catch (err) {
            failed.push(id);
            onError(id, err);
        }
    }
    return failed;
}
