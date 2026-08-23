# Matt's Laboratory Homepage Integration

The tracked homepage source lives at `homepage/index.html`.

HCS exposes four local LLM tools:

- `homepage_status`
- `read_homepage_source`
- `patch_homepage_source`
- `replace_homepage_source`

The model should read before writing and pass the returned SHA-256 as `expected_sha256`. Writes are atomic and create timestamped backups under `data/homepage_backups/` when HCS is running.

The GUI loads this file into the **Home** tab using `tkinterweb`. If the embedded renderer cannot initialize, HCS keeps the tab and offers **Open in Browser** as a fallback.
