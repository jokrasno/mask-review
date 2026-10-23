"""Per-user desktop paths and tightly scoped private filesystem permissions."""
import os
from pathlib import Path
import stat
import sys


def default_paths():
    home = Path.home()
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', str(home))) / 'MaskReview'
        return base / 'data', base / 'private'
    if sys.platform == 'darwin':
        base = home / 'Library' / 'Application Support' / 'MaskReview'
        return base / 'data', base / 'private'
    if sys.platform.startswith('linux'):
        def xdg(name, fallback):
            value = Path(os.environ.get(name, ''))
            return value if value.is_absolute() else fallback
        return (xdg('XDG_DATA_HOME', home / '.local' / 'share') / 'mask-review',
                xdg('XDG_CONFIG_HOME', home / '.config') / 'mask-review' / 'private')
    base = home / '.mask-review'
    return base / 'data', base / 'private'


def private_directory(path):
    """Touch only the selected leaf, never chmod its parents or another profile."""
    path = Path(path).absolute()
    if os.name == 'nt':
        path.mkdir(parents=True, exist_ok=True)
        return path
    home = Path.home().resolve()
    resolved = path.resolve()
    if (path.is_symlink() or resolved == home or resolved in home.parents
            or str(resolved) in ('/tmp', '/var/tmp', '/etc', '/usr', '/var', '/opt', '/Applications')):
        raise ValueError('Choose a dedicated private folder for Mask Review credentials.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('The private folder must be a directory owned by your user.')
    path.chmod(0o700)
    return path


def private_write(path, raw):
    """Atomic owner-only file creation, including the temporary file."""
    import uuid
    path = Path(path)
    private_directory(path.parent)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
