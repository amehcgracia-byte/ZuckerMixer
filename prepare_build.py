"""Build metadata and dependency license inventory shared by desktop packages."""
from pathlib import Path
import datetime,importlib.metadata,json,os,subprocess,shutil
root=Path(__file__).resolve().parent
version=(root/'VERSION').read_text().strip()
revision=os.environ.get('ZUCKER_SOURCE_REVISION_OVERRIDE') or os.environ.get('ZUCKER_SOURCE_REVISION')
if not revision:
    try:
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.CalledProcessError):
        revision_file=root/'SOURCE_REVISION.txt'
        revision=revision_file.read_text().strip() if revision_file.exists() else 'source-archive'

build=root/'build';build.mkdir(exist_ok=True)
(build/'build_metadata.json').write_text(json.dumps(dict(app_version=version,source_revision=revision,build_timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()),indent=2),encoding='utf-8')
licenses=build/'third-party-licenses';licenses.mkdir(exist_ok=True)
for dist in importlib.metadata.distributions():
    for file in dist.files or []:
        if any(x.lower().startswith(('license','copying','copyright','notice')) for x in file.parts):
            origin=Path(dist.locate_file(file))
            if origin.is_file():
                destination=licenses/dist.metadata['Name']/str(file)
                destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(origin,destination)
