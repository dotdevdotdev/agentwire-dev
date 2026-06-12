#!/bin/bash
# AgentWire Permission Hook for Claude Code
#
# This script is called by Claude Code's hook system when a permission
# check is needed. It reads the permission request JSON from stdin,
# posts it to the AgentWire portal, and returns the decision.
#
# Session detection (no env vars required):
#   1. .agentwire.yml in current or parent directory
#   2. Infer from directory path (~/projects/{session})
#   3. tmux session name

set -e

# Read JSON from stdin
input=$(cat)

# Honor session-level permission bypass — when Claude Code was launched with
# --dangerously-skip-permissions (permission_mode: "bypassPermissions") or is
# running in autonomous mode (permission_mode: "auto"), the user has explicitly
# opted out of permission friction. Short-circuit with an immediate allow so
# the portal is never consulted. Python used for JSON parsing — no jq dep,
# matches the stack the damage-control hooks run on.
permission_mode=$(printf '%s' "$input" | python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('permission_mode',''))
except Exception:
    pass" 2>/dev/null || true)

case "$permission_mode" in
    bypassPermissions|auto)
        echo '{"decision":"allow","message":"bypass mode: permission auto-approved"}'
        exit 0
        ;;
esac

# Find .agentwire.yml in current or parent directories
find_agentwire_config() {
    local dir="$PWD"
    local home="$HOME"
    while [ "$dir" != "/" ] && [ "$dir" != "$home" ]; do
        if [ -f "$dir/.agentwire.yml" ]; then
            echo "$dir/.agentwire.yml"
            return 0
        fi
        dir=$(dirname "$dir")
    done
    return 1
}

# Infer session from directory path
infer_session_from_path() {
    local cwd="$PWD"
    local projects_dir="$HOME/projects"
    if [[ "$cwd" == "$projects_dir"* ]]; then
        local rel_path="${cwd#$projects_dir/}"
        if [[ "$rel_path" =~ ^([^/]+)-worktrees/([^/]+) ]]; then
            echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
            return 0
        fi
        local project=$(echo "$rel_path" | cut -d'/' -f1)
        if [ -n "$project" ]; then
            echo "$project"
            return 0
        fi
    fi
    return 1
}

# Get session name
get_session() {
    # 1. tmux session name (most reliable - actual runtime context)
    if [ -n "$TMUX" ]; then
        tmux display-message -p '#S' 2>/dev/null
        return
    fi
    # 2. Infer from directory path (fallback when not in tmux)
    local inferred=$(infer_session_from_path)
    if [ -n "$inferred" ]; then
        echo "$inferred"
        return
    fi
    echo ""
}

session=$(get_session)
if [ -z "$session" ]; then
    echo '{"decision": "deny", "message": "Could not determine session (not in tmux, not in project dir)"}' >&2
    exit 1
fi

# Enrich the payload with pane context (#276: lets the portal route the
# prompt to a parent session and answer the right pane). Best-effort —
# any failure leaves the original payload untouched.
pane_index="0"
tmux_session=""
if [ -n "$TMUX_PANE" ]; then
    pane_index=$(tmux display -t "$TMUX_PANE" -p '#{pane_index}' 2>/dev/null || echo "0")
    tmux_session=$(tmux display -t "$TMUX_PANE" -p '#{session_name}' 2>/dev/null || echo "")
fi
enriched=$(printf '%s' "$input" | PANE_INDEX="$pane_index" TMUX_SESSION="$tmux_session" python3 -c "
import json, os, sys
d = json.load(sys.stdin)
d['pane_index'] = int(os.environ.get('PANE_INDEX') or 0)
d['tmux_session'] = os.environ.get('TMUX_SESSION', '')
print(json.dumps(d))" 2>/dev/null)
[ -n "$enriched" ] && input="$enriched"

# Get portal URL from global config
# Priority: 1. ~/.agentwire/portal_url file, 2. config.yaml server section, 3. default
get_portal_url() {
    # Explicit portal_url file takes priority
    if [ -f "$HOME/.agentwire/portal_url" ]; then
        cat "$HOME/.agentwire/portal_url" | tr -d '\n'
        return
    fi
    # Try to read server.port from config.yaml
    if [ -f "$HOME/.agentwire/config.yaml" ]; then
        # Check if SSL is configured (look for cert under ssl section)
        local has_ssl=$(grep -A2 "^\s*ssl:" "$HOME/.agentwire/config.yaml" 2>/dev/null | grep -E "cert:" || true)
        local port=$(grep -E "^\s*port:" "$HOME/.agentwire/config.yaml" 2>/dev/null | head -1 | sed 's/.*port:[[:space:]]*//' | tr -d '"'"'" || true)
        if [ -n "$port" ]; then
            if [ -n "$has_ssl" ]; then
                echo "https://localhost:$port"
            else
                echo "http://localhost:$port"
            fi
            return
        fi
    fi
    # Default
    echo "https://localhost:8765"
}

base_url=$(get_portal_url)

# Portal auth token: server.auth_token in config.yaml overrides the token file
get_portal_token() {
    if [ -f "$HOME/.agentwire/config.yaml" ]; then
        local override=$(grep -E "^\s*auth_token:" "$HOME/.agentwire/config.yaml" 2>/dev/null | head -1 | sed 's/.*auth_token:[[:space:]]*//' | tr -d '"'"'" || true)
        if [ -n "$override" ]; then
            echo "$override"
            return
        fi
    fi
    if [ -f "$HOME/.agentwire/portal.token" ]; then
        tr -d '\n' < "$HOME/.agentwire/portal.token"
    fi
}

token=$(get_portal_token)
auth_args=()
[ -n "$token" ] && auth_args=(-H "Authorization: Bearer $token")

# POST to portal and wait for response. 310s: must outlive the server's own
# 300s wait so its timeout-deny JSON reaches us instead of a curl abort.
response=$(curl -s -X POST "${base_url}/api/permission/${session}" \
    -H "Content-Type: application/json" \
    "${auth_args[@]}" \
    -d "$input" \
    --max-time 310 \
    --insecure 2>/dev/null)

# Check if curl succeeded
if [ $? -ne 0 ]; then
    echo '{"decision": "deny", "message": "Failed to connect to AgentWire portal"}' >&2
    exit 1
fi

# Return the response
echo "$response"
