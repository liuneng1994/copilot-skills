#!/bin/bash
# Restore Copilot skills and plugins on a new machine.
# Usage: git clone https://github.com/liuneng1994/copilot-skills.git ~/.copilot/skills && ~/.copilot/skills/setup.sh
set -e

COPILOT_HOME="${COPILOT_HOME:-$HOME/.copilot}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Copilot Skills Setup ==="

# 1. Ensure skills dir is linked/cloned correctly
if [ "$(realpath "$COPILOT_HOME/skills")" != "$(realpath "$SCRIPT_DIR")" ]; then
    echo "Warning: $COPILOT_HOME/skills does not point to this repo."
    echo "Consider: ln -sf $SCRIPT_DIR $COPILOT_HOME/skills"
fi

# 2. Restore plugins from manifest
MANIFEST="$SCRIPT_DIR/plugins.json"
if [ ! -f "$MANIFEST" ]; then
    echo "No plugins.json found, skipping plugin restore."
    exit 0
fi

echo ""
echo "=== Restoring Plugins ==="

# Read marketplace and plugin info
python3 - "$MANIFEST" "$COPILOT_HOME" <<'PYEOF'
import json, sys, os, subprocess

manifest_path = sys.argv[1]
copilot_home = sys.argv[2]
config_path = os.path.join(copilot_home, "config.json")

manifest = json.load(open(manifest_path))

# Load or create config
if os.path.exists(config_path):
    config = json.load(open(config_path))
else:
    config = {}

# Merge marketplaces
existing_mp = config.get("marketplaces", {})
for name, info in manifest.get("marketplaces", {}).items():
    if name not in existing_mp:
        existing_mp[name] = info
        print(f"  Added marketplace: {name}")
config["marketplaces"] = existing_mp

# Merge plugins
existing_plugins = {p["name"]: p for p in config.get("installed_plugins", [])}
for plugin in manifest.get("plugins", []):
    pname = plugin["name"]
    mp_name = plugin["marketplace"]

    if pname in existing_plugins:
        print(f"  Plugin already installed: {pname}")
        continue

    # Clone the plugin repo if needed
    mp_info = manifest["marketplaces"].get(mp_name, {})
    source = mp_info.get("source", {})
    if source.get("source") == "github":
        repo = source["repo"]
        clone_dir = os.path.join(copilot_home, "installed-plugins", mp_name, pname)
        if not os.path.exists(clone_dir):
            os.makedirs(os.path.dirname(clone_dir), exist_ok=True)
            print(f"  Cloning {repo} -> {clone_dir}")
            subprocess.run(
                ["git", "clone", "--depth=1", f"https://github.com/{repo}.git", clone_dir],
                check=True
            )
        plugin["cache_path"] = clone_dir
        existing_plugins[pname] = plugin
        print(f"  Installed plugin: {pname} from {repo}")

config["installed_plugins"] = list(existing_plugins.values())

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print("\nDone! Restart Copilot CLI to load plugins.")
PYEOF

echo ""
echo "=== Setup Complete ==="
