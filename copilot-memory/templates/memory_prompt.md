### Relevant memory

Use the following memory only as compact guidance. Prefer fresher items with higher confidence. If two memories conflict, surface the conflict instead of silently choosing one.

{{prompt_block}}

### Usage rules

- Treat `repo` memory as repository-scoped only.
- Treat `user` memory as preference hints, not hard requirements.
- Never expose secrets or sensitive raw text from memory.
- Do not quote memory IDs unless they help with debugging the memory system itself.
- If no relevant memory exists, continue normally without inventing one.
