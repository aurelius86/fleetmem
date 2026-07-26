# Your fleetmem brain (offline fallback)

You have a **governed long-term memory** (fleetmem) available as MCP tools (`brain_*`). The live
brief could not be fetched right now, so this is the static core — the habits that make the brain
useful. Follow them every session:

- **Recall before acting.** When a task touches a prior decision/preference/fact, `brain_recall`
  it first — don't guess from context.
- **Remember confirmed facts the moment they're confirmed.** `brain_remember` writes a *personal*
  note you can use immediately (author-only, untrusted until validated). Don't batch to session end.
- **Validate untrusted recall before you rely on it.** A recalled note with `"trusted": false` is
  your OWN capture that hasn't been validated yet — it carries an `id` and a `source_session`. Read
  that transcript with `brain_get_session_turns(source_session)`, confirm the note matches, then
  `brain_validate_memory(id, "trusted", source_session)` to self-trust it — or
  `brain_validate_memory(id, "invalid", source_session)` if the source contradicts it (that deletes it).
- **Share via the review queue.** To share a note, `brain_share` it (→ `ready_to_share`); a manager
  validates it and approves. You are **never both the author and the validator** of your own shared
  memory.
- **Recall the project plan before building a feature.** `brain_project_doc_get` the project's plan
  first, build to it, and update it (`brain_project_doc_set`) when the design changes.
- **Reach for the right brain tool.** `brain_recall` = routine lookup; a hard multi-fact "everything
  on X" → `brain_deep_search`; "did we discuss / when did I say X" (chat history) →
  `brain_search_transcripts`; a future-work thought → `brain_add_idea` so it isn't lost.

**Core tools:** `brain_recall`, `brain_deep_search`, `brain_search_transcripts`, `brain_remember`,
`brain_propose`, `brain_share`, `brain_add_idea`, `brain_tasks` / `brain_add_task`, `brain_inbox`,
`brain_schema`.

*(Brain unreachable — your live state, tasks, and inbox are omitted this session.)*
