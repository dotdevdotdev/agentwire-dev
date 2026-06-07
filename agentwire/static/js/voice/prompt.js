/**
 * Shared voice prompt wrapper — single source for the prefix that tells the
 * agent a message arrived by voice and how to reply audibly.
 */

export function voicePromptWrap(text) {
    return `[User said: '${text}' - respond using MCP tool: agentwire_say(text="your message")]`;
}
