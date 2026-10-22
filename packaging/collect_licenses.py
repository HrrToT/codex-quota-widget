from pathlib import Path
from importlib import metadata
import shutil
import sys

out = Path('dist/CodexQuotaWidget/licenses')
out.mkdir(parents=True, exist_ok=True)
base = Path(sys.base_prefix)
for name in ('LICENSE.txt', 'LICENSE'):
    source = base / name
    if source.is_file():
        shutil.copy2(source, out / ('Python-' + name))
for package in ('Pillow', 'PyInstaller'):
    dist = metadata.distribution(package)
    for entry in dist.files or []:
        if 'license' in str(entry).lower() or 'copying' in str(entry).lower():
            source = Path(dist.locate_file(entry))
            if source.is_file() and source.suffix.lower() not in ('.py', '.pyc'):
                shutil.copy2(source, out / (package + '-' + source.name))
for source in Path('dist/CodexQuotaWidget/_internal').rglob('license.terms'):
    shutil.copy2(source, out / (source.parent.name + '-license.terms'))
