# The unattended contract (#1309)

Neither agentwire nor `damage-control-dev` relies on the other. This page is the whole of what agentwire owns here; code is `agentwire/unattended.py`.

## `AGENTWIRE_UNATTENDED=1` — "no human is present"

The scheduler sets it in the environment of every dispatch. New sessions created from a dispatch inherit it (`core._with_unattended_env`), so children of an unattended run are unattended too. It is stripped from the CLI's own process environment at import so a stray `tmux` server never carries it into interactive sessions (#674).

It is a **public signal**: agentwire itself reads it (e.g. `agentwire tasks promote` refuses under it), and any other tool running in the session — a permission hook, a wrapper — may read it. It means one thing only: nobody is at the keyboard to answer a prompt.

## `AGENTWIRE_UNATTENDED_ALLOW` — opaque grants

A task's `unattended_allow` list (in `.agentwire.tasks.yml`) is **passed through, not interpreted**. agentwire does no validation of the entries; `agentwire tasks review` does not check them. The list is serialized by `unattended.encode_allow` into one string: comma-separated when every entry is a string, compact JSON when any entry is structured.

Whatever reads that variable owns its grammar, its validation, and its precedence rules. In particular, the guard engine owns grant validation and takes the variable names from its own configuration (damage-control-dev #63); agentwire only guarantees that these two names carry the marker and the serialized list.
