export const meta = {
  name: 'wf-spike',
  description: 'Three-agent probe: two read-only agents and one that edits inside an isolated worktree',
}

phase('Probe')

const results = await parallel([
  () => agent(
    'Run `ls -la` in the project root, then run `sleep 20`, then report the number of files you saw.',
    { label: 'ls-agent' },
  ),
  () => agent(
    'Read README.md, run `sleep 25`, then report its first line.',
    { label: 'read-agent' },
  ),
  () => agent(
    'Work in your own isolated git worktree copy of this repo, never in the main checkout. Append the line "spike" to notes.txt there, commit it, run `sleep 30`, then report `pwd`, `git branch --show-current` and `git rev-parse --show-toplevel`.',
    { label: 'edit-agent' },
  ),
])

return results
