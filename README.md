# Copilot Skills

This repository packages a set of agent skills as a TRAE CLI plugin marketplace.

## TRAE CLI Marketplace Layout

- `.agents/plugins/marketplace.json` declares the marketplace.
- `plugins/copilot-skills/.codex-plugin/plugin.json` declares the plugin.
- `plugins/copilot-skills/skills/` contains the bundled skills.

The marketplace entry points at the local plugin path:

```json
{
  "name": "copilot-skills",
  "source": {
    "source": "local",
    "path": "./plugins/copilot-skills"
  }
}
```

## Included Skills

- `ado-pipelines`
- `bloop-test`
- `brainstorming`
- `copilot-memory`
- `design-state-machine`
- `find-skills`
- `ocp-perf`
- `using-superpowers`

## Legacy Copilot Files

`setup.sh`, `plugins.json`, and `copilot-instructions.md` are retained for the
original Copilot setup flow. TRAE CLI loads the plugin from the marketplace and
plugin manifests above.
