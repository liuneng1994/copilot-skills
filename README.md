# Copilot Skills

This repository packages a set of agent skills as a TRAE CLI plugin marketplace.
Each skill is published as its own plugin so TRAE CLI can install or enable them
selectively.

## TRAE CLI Marketplace Layout

- `.agents/plugins/marketplace.json` declares the marketplace.
- `plugins/<plugin-name>/.codex-plugin/plugin.json` declares each plugin.
- `plugins/<plugin-name>/skills/<skill-name>/` contains that plugin's skill.

Marketplace entries point at local plugin paths:

```json
{
  "name": "ado-pipelines",
  "source": {
    "source": "local",
    "path": "./plugins/ado-pipelines"
  }
}
```

## Included Plugins

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
original Copilot setup flow. TRAE CLI loads selectable plugins from the
marketplace and plugin manifests above.
