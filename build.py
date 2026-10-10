"""在安装 requirements-build.txt 后运行：python build.py。"""
import argparse
import os
import sys
from pathlib import Path
import PyInstaller.__main__

source = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--work-dir', type=Path, default=source.parent/'build-work')
args = parser.parse_args()
work = args.work_dir.resolve()
work.mkdir(parents=True, exist_ok=True)
os.environ['PYINSTALLER_CONFIG_DIR'] = str(work/'config')
extra = []
conda_bin = Path(sys.base_prefix)/'Library'/'bin'
if conda_bin.is_dir():
    os.environ['PATH'] = str(conda_bin)+os.pathsep+os.environ.get('PATH','')
    for name in ('ffi.dll', 'tk86t.dll', 'tcl86t.dll', 'libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll'):
        path = conda_bin/name
        if path.exists():
            extra.extend(['--add-binary', str(path)+';.'])
PyInstaller.__main__.run([
    '--noconfirm', '--clean', '--onefile', '--windowed', '--uac-admin',
    '--name', '奥比岛修炼助手', '--icon', str(source/'app.ico'),
    '--add-data', str(source/'assets')+';assets',
    '--add-data', str(source/'ocr_worker.ps1')+';.',
    '--add-data', str(source/'app.ico')+';.',
    '--distpath', str(source.parent), '--workpath', str(work/'cache'),
    '--specpath', str(work), *extra, str(source/'aobi_gui.py'),
])
