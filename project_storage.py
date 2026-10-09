"""Place per-session data beside sessions, without moving recordings or exports."""
from pathlib import Path
import hashlib
import json
import shutil
import uuid


def project_directory(source):
    source = Path(source).expanduser().resolve()
    sessions = next((p for p in source.parents if p.name.casefold() == 'zzsesions'), None)
    raw = next((p for p in source.parents if p.name.casefold() == 'raw'), None)
    session = raw.parent.name if raw else source.name
    key = hashlib.sha256(str(source).encode()).hexdigest()[:20]
    root = (sessions if sessions else source.parent) / 'ZuckerMixer'
    return root / f'{session}-{key}'


def _inventory(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Project state contains a symbolic link: {path}')
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            result[str(path.relative_to(root))] = digest.hexdigest()
    return result


def migrate_source_state(source, legacy):
    """Publish a checked copy before removing legacy data; call while idle."""
    project = project_directory(source)
    target = project / '.zuckermixer'
    if target.exists():
        return target
    project.mkdir(parents=True, exist_ok=True)
    legacy = Path(legacy)
    stage = project / f'.migrating-{uuid.uuid4().hex}'
    try:
        if legacy.exists():
            original = _inventory(legacy)
            shutil.copytree(legacy, stage)
            if _inventory(stage) != original or _inventory(legacy) != original:
                raise RuntimeError('Project data changed during migration; original retained')
        else:
            stage.mkdir()
        # Analysis plans may contain absolute paths into the old private state.
        # Rewrite only that prefix; original recordings and exports keep theirs.
        def relocate(value):
            if isinstance(value, str) and value.startswith(str(legacy) + '/'):
                return str(target / Path(value).relative_to(legacy))
            if isinstance(value, list):
                return [relocate(item) for item in value]
            if isinstance(value, dict):
                return {key: relocate(item) for key, item in value.items()}
            return value
        for file in stage.glob('*.json'):
            payload = json.loads(file.read_text())
            rewritten = relocate(payload)
            if rewritten != payload:
                file.write_text(json.dumps(rewritten, indent=2))
        stage.rename(target)
        (project / 'project.json').write_text(json.dumps({'source_folder': str(Path(source).resolve()), 'state_folder': '.zuckermixer'}, indent=2))
        if legacy.exists():
            shutil.rmtree(legacy)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)
