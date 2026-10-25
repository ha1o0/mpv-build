#!/usr/bin/env python3
"""Configure and validate the Linux zcopy build without changing generic builds."""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def configuration():
    return json.loads((ROOT / 'linux-build.json').read_text(encoding='utf8'))


def run(command, cwd=None, check=True):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding='utf8', errors='replace', timeout=180)
    if check and result.returncode:
        raise RuntimeError(' '.join(map(str, command)) + '\n' + result.stdout + result.stderr)
    return result


def apply_patch(source, patch):
    # Both directions check all hunks. A partial or incompatible upstream port
    # must fail rather than be accepted based on an option/header name alone.
    reverse = run(['git', 'apply', '--reverse', '--check', str(patch)], source, False)
    if not reverse.returncode:
        return 'already present'
    run(['git', 'apply', '--check', '--whitespace=error-all', str(patch)], source)
    run(['git', 'apply', '--whitespace=error-all', str(patch)], source)
    return 'applied'


def apply_series(source, component):
    patches = [ROOT / relative for relative in configuration()['patches'][component]]
    # Later patches can change an earlier patch's context. Check the complete
    # series in a scratch tree, reversing in the opposite order. No real source
    # is touched until the entire forward preflight succeeds.
    names = set()
    for patch in patches:
        names.update(re.findall(r'^\+\+\+ b/(.+)$', patch.read_text(encoding='utf8'), re.M))
    original = {name: (source / name).read_bytes() if (source / name).exists() else None
                for name in names}

    def trial(reverse):
        with tempfile.TemporaryDirectory(prefix='linux-patch-series-') as directory:
            scratch = Path(directory)
            run(['git', 'init', '--quiet'], scratch)
            for name, content in original.items():
                if content is not None:
                    path = scratch / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            for patch in reversed(patches) if reverse else patches:
                command = ['git', 'apply', '--whitespace=error-all']
                if reverse:
                    command += ['--reverse']
                result = run(command + ['--check', str(patch)], scratch, False)
                if result.returncode:
                    return False, result.stderr
                run(command + [str(patch)], scratch)
        return True, ''

    if trial(True)[0]:
        print(component + ': complete patch series already present', flush=True)
        return
    ready, error = trial(False)
    if not ready:
        raise RuntimeError(component + ': incomplete or incompatible patch series\n' + error)
    current = {name: (source / name).read_bytes() if (source / name).exists() else None
               for name in names}
    if current != original:
        raise RuntimeError(component + ': source changed during patch preflight')
    for patch in patches:
        run(['git', 'apply', '--whitespace=error-all', str(patch)], source)
    print('{}: applied {} patches'.format(component, len(patches)), flush=True)


def configure(root, mpv_ref):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._/-]*', mpv_ref):
        raise ValueError('Invalid mpv branch, tag or commit')
    config = root / 'config'
    config.mkdir(exist_ok=True)
    (config / 'branch-mpv').write_text('@' + mpv_ref + '\n', encoding='utf8')
    (config / 'branch-ffmpeg').write_text('@' + configuration()['ffmpeg_ref'] + '\n', encoding='utf8')
    (config / 'branch-libplacebo').write_text('@' + configuration()['libplacebo_ref'] + '\n', encoding='utf8')
    # Preserve the existing codecs/encoders and FFmpeg Vulkan setting. The
    # required native rendering path is enabled separately in mpv/libplacebo.
    ffmpeg = ['--enable-gpl', '--enable-nonfree', '--enable-libx264', '--disable-vulkan']
    ffmpeg += ['--enable-decoder=' + value for value in
               ('rv10', 'rv20', 'rv30', 'rv40', 'cook', 'atrac3', 'sipr', 'ra_144', 'ra_288')]
    (root / 'ffmpeg_options').write_text('\n'.join(ffmpeg) + '\n', encoding='utf8')
    mpv = ['-Dlibmpv=true', '-Dlibcurl=enabled', '-Dgl=enabled',
           '-Dplain-gl=enabled', '-Degl=enabled']
    (root / 'mpv_options').write_text('\n'.join(mpv) + '\n', encoding='utf8')


def patch_sources(root):
    actual = run(['git', 'rev-parse', 'HEAD'], root / 'ffmpeg').stdout.strip()
    if actual != configuration()['ffmpeg_ref']:
        raise ValueError('FFmpeg source differs from the pinned MP4 patch target: ' + actual)
    required = ('demuxer-lavf-mp4-read-strategy',)
    lavf = (root / 'mpv/demux/demux_lavf.c').read_text(encoding='utf8')
    mkv = (root / 'mpv/demux/demux_mkv.c').read_text(encoding='utf8')
    if any(name not in lavf for name in required) or 'defer-cues' not in mkv or 'skip-tags' not in mkv:
        raise ValueError('Selected mpv source lacks the zcopy MP4/MKV features')
    apply_series(root / 'ffmpeg', 'ffmpeg')
    apply_series(root / 'mpv', 'mpv')


def check_curl_linkage(dependencies):
    matches = [entry for entry in dependencies if entry.get('name') == 'libcurl']
    if len(matches) != 1:
        raise ValueError('Expected one libcurl dependency in Meson introspection')
    curl = matches[0]
    if curl.get('version') != configuration()['curl']['version']:
        raise ValueError('Meson did not select the pinned curl version')
    arguments = curl.get('link_args', [])
    archives = [arg for arg in arguments if re.search(r'\.a(?:$|[,\s])', arg)
                or arg in ('-static', '-Wl,-Bstatic')]
    if archives:
        raise ValueError('Shared curl must not pull static private dependencies into libmpv: ' +
                         ', '.join(archives))
    if not any(arg == '-lcurl' or re.search(r'libcurl\.so(?:\.|$)', arg) for arg in arguments):
        raise ValueError('Meson libcurl link arguments do not select the shared library')
    return curl


def verify_link(root):
    path = root / 'mpv/build/meson-info/intro-dependencies.json'
    curl = check_curl_linkage(json.loads(path.read_text(encoding='utf8')))
    target = root / 'build-linux-validation'
    target.mkdir(exist_ok=True)
    (target / 'curl-linkage.json').write_text(json.dumps(curl, indent=2) + '\n', encoding='utf8')
    print('Verified Meson uses shared curl without static private dependencies.')


def verify(root):
    binary = root / 'mpv/build/mpv'
    common = [str(binary), '--no-config', '--load-scripts=no']
    options = run(common + ['--list-options']).stdout
    required = ('demuxer-lavf-mp4-read-strategy', 'demuxer-mkv-skip-tags',
                'demuxer-mkv-defer-cues', 'curl-enabled', 'curl-reconnect-streamed',
                'curl-retry-delay-max', 'image-subs-colorspace')
    for option in required:
        if '--' + option not in options:
            raise ValueError('Built mpv lacks required option: ' + option)
    outputs = run(common + ['--vo=help'])
    if 'gpu-next' not in outputs.stdout + outputs.stderr:
        raise ValueError('Built mpv lacks gpu-next')
    header = (root / 'mpv/include/mpv/render.h').read_text(encoding='utf8')
    if 'MPV_RENDER_PARAM_BACKEND = 21' not in header:
        raise ValueError('Custom gpu-next Render API ABI is missing')
    help_mov = run([str(root / 'ffmpeg_build/ffmpeg'), '-hide_banner', '-h', 'demuxer=mov'])
    for option in ('interleave_strategy', 'interleave_preload_duration', 'interleave_preload_max_bytes'):
        if option not in help_mov.stdout + help_mov.stderr:
            raise ValueError('Built FFmpeg lacks the MP4 companion option: ' + option)
    lib = C.CDLL(str(root / 'mpv/build/libmpv.so'))
    lib.mpv_create.restype = C.c_void_p
    lib.mpv_set_option_string.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p]
    lib.mpv_initialize.argtypes = [C.c_void_p]
    lib.mpv_get_property_string.argtypes = [C.c_void_p, C.c_char_p]
    lib.mpv_get_property_string.restype = C.c_void_p
    lib.mpv_free.argtypes = [C.c_void_p]
    lib.mpv_terminate_destroy.argtypes = [C.c_void_p]
    handle = lib.mpv_create()
    if not handle:
        raise RuntimeError('mpv_create failed')
    defaults = {}
    try:
        for key, value in (('config', 'no'), ('load-scripts', 'no'), ('vo', 'null'), ('ao', 'null')):
            if lib.mpv_set_option_string(handle, key.encode(), value.encode()) < 0:
                raise RuntimeError('Cannot set verification option: ' + key)
        if lib.mpv_initialize(handle) < 0:
            raise RuntimeError('mpv_initialize failed')
        for name, expected in (('curl-enabled', 'yes'), ('demuxer-lavf-mp4-read-strategy', 'auto'),
                               ('demuxer-mkv-skip-tags', 'no'), ('demuxer-mkv-defer-cues', 'no')):
            pointer = lib.mpv_get_property_string(handle, ('options/' + name).encode())
            if not pointer:
                raise RuntimeError('Cannot read native option: ' + name)
            try:
                defaults[name] = C.string_at(pointer).decode()
            finally:
                lib.mpv_free(pointer)
            if defaults[name] != expected:
                raise ValueError('{} default is {}, expected {}'.format(name, defaults[name], expected))
    finally:
        lib.mpv_terminate_destroy(handle)
    result = {'mpv': run(common + ['--version']).stdout, 'defaults': defaults,
              'libmpv_sha256': hashlib.sha256((root / 'mpv/build/libmpv.so').read_bytes()).hexdigest(),
              'required_options': required, 'render_backend_abi': 21}
    target = root / 'build-linux-validation'
    target.mkdir(exist_ok=True)
    (target / 'features.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf8')
    print('Verified gpu-next, curl, MP4 extension, MKV options and unchanged MKV defaults.')


def provenance(root, destination):
    features = root / 'build-linux-validation/features.json'
    result = json.loads(features.read_text(encoding='utf8'))
    actual = hashlib.sha256((root / 'mpv/build/libmpv.so').read_bytes()).hexdigest()
    if actual != result.get('libmpv_sha256'):
        raise ValueError('The library changed after validation; rerun verification before packaging')
    shutil.copy2(root / 'linux-build.json', destination)
    shutil.copy2(features, destination)
    license_dir = root / 'build_libs/share/licenses/curl'
    shutil.copytree(license_dir, destination / 'licenses/curl', dirs_exist_ok=True)
    with (destination / 'manifest.txt').open('a', encoding='utf8') as manifest:
        manifest.write('validated_libmpv_sha256=' + actual + '\n')
        manifest.write('curl=' + configuration()['curl']['version'] + '\n')
        for patches in configuration()['patches'].values():
            for relative in patches:
                digest = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                manifest.write('patch.' + Path(relative).name + '=' + digest + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('configure', 'patch', 'verify-link', 'verify', 'provenance'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--mpv-ref', default='zcopy')
    parser.add_argument('--package-root', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'configure':
        configure(root, args.mpv_ref)
    elif args.action == 'patch':
        patch_sources(root)
    elif args.action == 'verify-link':
        verify_link(root)
    elif args.action == 'verify':
        verify(root)
    else:
        if not args.package_root:
            parser.error('provenance requires --package-root')
        provenance(root, args.package_root.resolve())


if __name__ == '__main__':
    main()
