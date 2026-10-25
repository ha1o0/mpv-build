#!/usr/bin/env python3
"""Build pinned shared curl for the Linux bundle, with HTTP/2 and system TLS."""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    root = args.root.resolve()
    pin = json.loads((root / 'linux-build.json').read_text())['curl']
    work = root / 'build-linux-curl'
    work.mkdir(exist_ok=True)
    archive = work / ('curl-' + pin['version'] + '.tar.gz')
    if not archive.exists():
        with urllib.request.urlopen(pin['url'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != pin['sha256']:
            raise ValueError('curl download checksum mismatch')
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin['sha256']:
        raise ValueError('Cached curl checksum mismatch')
    source = work / ('curl-' + pin['version'])
    # The pinned archive is verified before extraction. Reject unsafe paths
    # and links on Python 3.10 too (the Ubuntu 22.04 runner's Python version).
    if not source.exists():
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                path = (work / member.name).resolve()
                if not path.is_relative_to(work) or member.issym() or member.islnk() or member.isdev():
                    raise ValueError('Unsafe curl archive member: ' + member.name)
            package.extractall(work)
    prefix = root / 'build_libs'
    build = work / 'build'
    options = [
        '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_INSTALL_PREFIX=' + str(prefix),
        '-DCMAKE_INSTALL_LIBDIR=lib', '-DCMAKE_INSTALL_RPATH=$ORIGIN',
        '-DBUILD_SHARED_LIBS=ON', '-DBUILD_STATIC_LIBS=OFF', '-DBUILD_CURL_EXE=OFF',
        '-DBUILD_TESTING=OFF', '-DBUILD_EXAMPLES=OFF', '-DBUILD_LIBCURL_DOCS=OFF',
        '-DBUILD_MISC_DOCS=OFF', '-DENABLE_CURL_MANUAL=OFF',
        '-DCURL_USE_OPENSSL=ON', '-DUSE_NGHTTP2=ON',
        '-DCMAKE_REQUIRE_FIND_PACKAGE_NGHTTP2=ON', '-DCURL_USE_PKGCONFIG=ON',
    ]
    subprocess.run(['cmake', '-S', str(source), '-B', str(build), '-G', 'Ninja'] + options, check=True)
    subprocess.run(['cmake', '--build', str(build), '--parallel', str(max(1, args.jobs))], check=True)
    subprocess.run(['cmake', '--install', str(build)], check=True)
    class Version(C.Structure):
        _fields_ = [('age', C.c_int), ('version', C.c_char_p), ('version_num', C.c_uint),
                    ('host', C.c_char_p), ('features', C.c_int), ('ssl_version', C.c_char_p)]
    lib = C.CDLL(str(prefix / 'lib/libcurl.so'))
    lib.curl_version_info.argtypes = [C.c_int]
    lib.curl_version_info.restype = C.POINTER(Version)
    info = lib.curl_version_info(0).contents
    if info.version.decode() != pin['version'] or not info.features & (1 << 16) or not info.features & (1 << 2):
        raise ValueError('curl must match the pin and support HTTP/2 and TLS')
    license_dir = prefix / 'share/licenses/curl'
    license_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'COPYING', license_dir)
    print('Verified curl {}: HTTP/2, TLS {}'.format(info.version.decode(), info.ssl_version.decode()))


if __name__ == '__main__':
    main()
