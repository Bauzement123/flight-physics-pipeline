#!/usr/bin/env python3
"""Devtool: Scan imports in the codebase and check installed versions on the system."""
import ast
import logging
import sys
from pathlib import Path

from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)

# Python stdlib modules (fallback list if sys.stdlib_module_names not available)
STDLIB_FALLBACK = {
    'abc', 'argparse', 'ast', 'asyncio', 'base64', 'binascii', 'bisect', 'builtins',
    'calendar', 'cmath', 'cmd', 'code', 'codecs', 'collections', 'colorsys', 'compileall',
    'concurrent', 'configparser', 'contextlib', 'contextvars', 'copy', 'copyreg', 'crypt',
    'csv', 'ctypes', 'curses', 'dataclasses', 'datetime', 'dbm', 'decimal', 'difflib',
    'dis', 'distutils', 'doctest', 'email', 'encodings', 'ensurepip', 'enum', 'errno',
    'faulthandler', 'filecmp', 'fileinput', 'fnmatch', 'fractions', 'ftplib', 'functools',
    'gc', 'getopt', 'getpass', 'gettext', 'glob', 'graphlib', 'grp', 'gzip', 'hashlib',
    'heapq', 'hmac', 'html', 'http', 'imaplib', 'imghdr', 'importlib', 'inspect', 'io',
    'ipaddress', 'itertools', 'json', 'keyword', 'lib2to3', 'linecache', 'locale', 'logging',
    'lzma', 'mailbox', 'mailcap', 'marshal', 'math', 'mimetypes', 'mmap', 'modulefinder',
    'multiprocessing', 'netrc', 'nis', 'nntplib', 'nt', 'ntpath', 'numbers', 'operator',
    'optparse', 'os', 'ossaudiodev', 'pathlib', 'pdb', 'pickle', 'pickletools', 'pipes',
    'pkgutil', 'platform', 'plistlib', 'poplib', 'posix', 'posixpath', 'pprint', 'profile',
    'pstats', 'pty', 'pwd', 'py_compile', 'pyclbr', 'pydoc', 'queue', 'quopri', 'random',
    're', 'readline', 'reprlib', 'resource', 'rlcompleter', 'runpy', 'sched', 'secrets',
    'select', 'selectors', 'shelve', 'shlex', 'shutil', 'signal', 'site', 'smtpd', 'smtplib',
    'sndhdr', 'socket', 'socketserver', 'spwd', 'sqlite3', 'ssl', 'stat', 'statistics',
    'string', 'stringprep', 'struct', 'subprocess', 'sunau', 'symtable', 'sys', 'sysconfig',
    'syslog', 'tabnanny', 'tarfile', 'telnetlib', 'tempfile', 'termios', 'test', 'textwrap',
    'threading', 'time', 'timeit', 'tkinter', 'token', 'tokenize', 'trace', 'traceback',
    'tracemalloc', 'tty', 'types', 'typing', 'unicodedata', 'unittest', 'urllib', 'uu',
    'uuid', 'venv', 'warnings', 'wave', 'weakref', 'webbrowser', 'winreg', 'winsound',
    'wsgiref', 'xdg', 'xml', 'xmlrpc', 'zipapp', 'zipfile', 'zipimport', 'zlib', '_thread',
    '_dummy_thread'
}

PIP_NAME_MAP = {
    'sklearn': 'scikit-learn',
    'yaml': 'pyyaml',
    'netCDF4': 'netcdf4',
    'PIL': 'pillow',
    'fitz': 'pymupdf',
    'git': 'gitpython',
    'jwt': 'pyjwt',
    'pg8000': 'pg8000',
    'OpenSSL': 'pyopenssl',
}

def get_stdlib_modules() -> set[str]:
    if hasattr(sys, 'stdlib_module_names'):
        return sys.stdlib_module_names
    return STDLIB_FALLBACK

def scan_imports(src_dir: Path) -> tuple[list[str], list[str]]:
    stdlib_imports = set()
    external_imports = set()
    stdlib = get_stdlib_modules()
    
    for py_file in src_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_module = alias.name.split('.')[0]
                        if not top_module or top_module.startswith('_') or top_module == 'src':
                            continue
                        if top_module in stdlib:
                            stdlib_imports.add(top_module)
                        else:
                            external_imports.add(top_module)
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        top_module = node.module.split('.')[0]
                        if not top_module or top_module.startswith('_') or top_module == 'src':
                            continue
                        if top_module in stdlib:
                            stdlib_imports.add(top_module)
                        else:
                            external_imports.add(top_module)
        except Exception:
            continue
            
    return sorted(list(stdlib_imports)), sorted(list(external_imports))

def get_installed_version(module_name: str) -> str:
    dist_name = PIP_NAME_MAP.get(module_name, module_name)
    
    try:
        import importlib.metadata
        dist_map = importlib.metadata.packages_distributions()
        dists = dist_map.get(module_name)
        if dists:
            return importlib.metadata.version(dists[0])
    except Exception:
        pass
        
    for name in [dist_name, module_name]:
        try:
            import importlib.metadata
            return importlib.metadata.version(name)
        except Exception:
            pass

    try:
        import importlib
        mod = importlib.import_module(module_name)
        for attr in ['__version__', 'version', '__git_version__']:
            if hasattr(mod, attr):
                return str(getattr(mod, attr))
    except Exception:
        pass

    return "Not Installed / Unknown"

def main() -> None:
    # Resolve the project root dynamically
    repo_root = Path(__file__).resolve().parent.parent.parent
    src_dir = repo_root / "src"
    python_version = sys.version.split()[0]
    
    logger.info(f"Scanning imports in {src_dir}...")
    stdlib_libs, external_libs = scan_imports(src_dir)
    
    logger.info("=========================================")
    logger.info("SECTION 1: Standard Library Imports Used")
    logger.info("=========================================")
    logger.info(f"{'Required Library (Import)':<30} | {'Installed Version':<20} | {'Status':<8}")
    logger.info("-" * 65)
    for lib in stdlib_libs:
        logger.info(f"{lib:<30} | {f'{python_version}':<20} | {'Installed':<8}")
        
    logger.info("")
    logger.info("=========================================")
    logger.info("SECTION 2: External Dependencies")
    logger.info("=========================================")
    logger.info(f"{'Required Library (Import)':<30} | {'Installed Version':<20} | {'Status':<8}")
    logger.info("-" * 65)
    for lib in external_libs:
        ver = get_installed_version(lib)
        status = "Installed" if ver != "Not Installed / Unknown" else "Missing"
        logger.info(f"{lib:<30} | {ver:<20} | {status:<8}")

if __name__ == "__main__":
    setup_file_logger(log_filename="devtools.log")
    main()
