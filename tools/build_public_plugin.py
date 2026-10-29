#!/usr/bin/env python3
"""Validate and package the generic plugin. Never copy runtime configuration."""
from pathlib import Path
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.github', '__pycache__', '.venv', 'dist', '.pytest_cache'}
REQUIRED = ['plugin.json', 'LICENSE', 'THIRD_PARTY.md', 'README.md',
            'setup.py', 'deploy_mini.py', 'keychain_store.py', 'run.py',
            'assets/icon.png', 'skills/proton-mail-bridge/SKILL.md',
            'docs/PRIVACY.md', 'docs/TERMS.md', 'docs/AGENT_PROMPT.md']


def validate():
    for name in REQUIRED:
        if not (ROOT / name).is_file():
            raise ValueError('Missing required file: ' + name)
    manifest = json.loads((ROOT / 'plugin.json').read_text())
    extension = manifest['extensions']['com.openai']
    interface = extension['interface']
    if manifest.get('apps') is not None or extension.get('apps') is not None:
        raise ValueError('Public setup plugin must not bind an account App')
    for key, limit in [('displayName', 30), ('shortDescription', 30), ('longDescription', 4000), ('developerName', 80)]:
        if not 0 < len(interface[key]) <= limit:
            raise ValueError('Invalid listing field: ' + key)
    prompts = interface['defaultPrompt']
    if not 1 <= len(prompts) <= 3 or any(not p.strip() or len(p) > 128 or '\n' in p for p in prompts):
        raise ValueError('Invalid default prompts')
    for key in ('websiteURL', 'supportURL', 'privacyPolicyURL', 'termsOfServiceURL'):
        if not interface[key].startswith('https://'):
            raise ValueError('Missing public HTTPS listing URL: ' + key)
    return manifest


def package():
    manifest = validate()
    files = []
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts) or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('Symlink is not allowed: ' + str(relative))
        if path.name in {'.app.json', '.mcp.json', 'mcp.json', '.env'} or path.suffix in {'.key', '.pem', '.log', '.pyc'}:
            raise ValueError('Private/runtime configuration is not allowed: ' + str(relative))
        if path.suffix != '.png':
            content = path.read_text()
            if re.search(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}', content):
                raise ValueError('Literal tunnel identity found: ' + str(relative))
            if re.search(r'(?:plugins_|asdk_app_)[0-9a-f]{20,}|/(?:Users)/|privaterelay\.appleid\.com', content):
                raise ValueError('Personal identity/path found: ' + str(relative))
            for token in re.findall(r'sk-[A-Za-z0-9_-]{35,}', content):
                if not token.startswith('sk-test-'):
                    raise ValueError('Potential API key literal found: ' + str(relative))
        files.append(path)
    target = ROOT / 'dist' / ('proton-mail-bridge-setup-' + manifest['version'] + '.zip')
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT))
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError('Archive verification failed')
    print(f'Validated {len(files)} public files; created {target.name}')


if __name__ == '__main__':
    package()
