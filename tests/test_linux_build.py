"""Offline contracts for source selection, safe patching and bundle provenance."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch as mock_patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('linux_build', ROOT / 'scripts/linux_build.py')
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

PATCH = '''diff --git a/options.c b/options.c
--- a/options.c
+++ b/options.c
@@ -1 +1 @@
-const int policy = 0;
+const int policy = 1;
diff --git a/policy.h b/policy.h
new file mode 100644
--- /dev/null
+++ b/policy.h
@@ -0,0 +1 @@
+#define BOUNDED_POLICY 1
'''


class LinuxBuildTests(unittest.TestCase):
    def test_shared_curl_does_not_import_private_static_archives(self):
        curl = {'name': 'libcurl', 'version': '8.23.0', 'type': 'pkgconfig',
                'link_args': ['/build/build_libs/lib/libcurl.so']}
        # FFmpeg/libplacebo remain static; only curl's dependency boundary is
        # checked. The observed libpsl.a/idn2 failure must be rejected early.
        static = {'name': 'libavcodec', 'link_args': ['/build/build_libs/lib/libavcodec.a']}
        self.assertEqual(build.check_curl_linkage([static, curl]), curl)
        observed = dict(curl, link_args=['/usr/lib/x86_64-linux-gnu/libpsl.a',
                                       '-L/build/build_libs/lib', '-lcurl'])
        with self.assertRaisesRegex(ValueError, 'static private dependencies'):
            build.check_curl_linkage([static, observed])

    def test_curl_linkage_cannot_silently_select_a_missing_old_or_static_library(self):
        for dependency in ([], [{'name': 'libcurl', 'version': '7.81.0', 'link_args': ['-lcurl']}],
                           [{'name': 'libcurl', 'version': '8.23.0', 'link_args': []}],
                           [{'name': 'libcurl', 'version': '8.23.0', 'link_args': ['/build/libcurl.a']}]):
            with self.subTest(dependency=dependency), self.assertRaises(ValueError):
                build.check_curl_linkage(dependency)

    def test_meson_link_check_records_the_resolved_shared_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = root / 'mpv/build/meson-info'
            info.mkdir(parents=True)
            dependency = {'name': 'libcurl', 'version': '8.23.0', 'type': 'pkgconfig',
                          'link_args': ['/build/build_libs/lib/libcurl.so']}
            (info / 'intro-dependencies.json').write_text(json.dumps([dependency]))
            build.verify_link(root)
            self.assertEqual(json.loads((root / 'build-linux-validation/curl-linkage.json').read_text()),
                             dependency)

    def test_specialized_configuration_selects_shared_core_and_keeps_legacy_codecs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build.configure(root, 'zcopy')
            self.assertEqual((root / 'config/branch-mpv').read_text().strip(), '@zcopy')
            self.assertEqual((root / 'config/branch-ffmpeg').read_text().strip(),
                             '@f1126f23380c805b4b8fd69351d189aeac8b892b')
            flags = (root / 'ffmpeg_options').read_text()
            for codec in ('rv10', 'rv20', 'rv30', 'rv40', 'cook', 'atrac3', 'sipr', 'ra_144', 'ra_288'):
                self.assertIn('--enable-decoder=' + codec, flags)
            self.assertIn('--enable-libx264', flags)
            self.assertIn('--disable-vulkan', flags)
            self.assertIn('-Dlibcurl=enabled', (root / 'mpv_options').read_text())

    def test_invalid_ref_is_rejected_before_any_configuration_is_written(self):
        for ref in ('-f', 'zcopy\n@master', '$(echo bad)', ''):
            with self.subTest(ref=ref), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ValueError):
                    build.configure(root, ref)
                self.assertEqual(list(root.iterdir()), [])

    def test_complete_patch_is_applied_once_and_second_application_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '--quiet'], cwd=root, check=True)
            (root / 'options.c').write_bytes(b'const int policy = 0;\n')
            patch = root / 'policy.patch'
            patch.write_bytes(PATCH.encode())
            self.assertEqual(build.apply_patch(root, patch), 'applied')
            expected = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            self.assertEqual(build.apply_patch(root, patch), 'already present')
            self.assertEqual(expected, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})

    def test_partial_or_changed_patch_fails_without_modifying_source(self):
        for content in (b'const int policy = 1;\n', b'const int policy = 7;\n'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(['git', 'init', '--quiet'], cwd=root, check=True)
                (root / 'options.c').write_bytes(content)
                patch = root / 'policy.patch'
                patch.write_bytes(PATCH.encode())
                with self.assertRaises(RuntimeError):
                    build.apply_patch(root, patch)
                self.assertEqual((root / 'options.c').read_bytes(), content)
                self.assertFalse((root / 'policy.h').exists())

    def test_overlapping_series_is_idempotent_and_partial_series_is_rejected(self):
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(['git', 'init', '--quiet'], cwd=root, check=True)
                subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=root, check=True)
                (root / 'options.c').write_bytes(b'const int policy = 0;\n')
                first, second = root / 'first.patch', root / 'second.patch'
                first.write_bytes(PATCH.encode())
                second.write_bytes(b'diff --git a/options.c b/options.c\n--- a/options.c\n+++ b/options.c\n@@ -1 +1 @@\n-const int policy = 1;\n+const int policy = 2;\n')
                pins = {'patches': {'mpv': [str(first), str(second)]}}
                with mock_patch.object(build, 'configuration', return_value=pins):
                    if partial:
                        build.apply_patch(root, first)
                        before = (root / 'options.c').read_bytes()
                        with self.assertRaisesRegex(RuntimeError, 'incomplete or incompatible'):
                            build.apply_series(root, 'mpv')
                        self.assertEqual((root / 'options.c').read_bytes(), before)
                    else:
                        build.apply_series(root, 'mpv')
                        self.assertEqual((root / 'options.c').read_bytes(), b'const int policy = 2;\n')
                        build.apply_series(root, 'mpv')
                        self.assertEqual((root / 'options.c').read_bytes(), b'const int policy = 2;\n')

    def test_stale_validation_cannot_be_attached_to_a_different_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mpv/build').mkdir(parents=True)
            (root / 'mpv/build/libmpv.so').write_bytes(b'changed library')
            (root / 'build-linux-validation').mkdir()
            (root / 'build-linux-validation/features.json').write_text(json.dumps({
                'libmpv_sha256': hashlib.sha256(b'old library').hexdigest()}))
            package = root / 'output'
            package.mkdir()
            with self.assertRaisesRegex(ValueError, 'changed after validation'):
                build.provenance(root, package)
            self.assertEqual(list(package.iterdir()), [])

    def test_fixed_commit_can_be_selected_from_a_shallow_clone(self):
        bash = os.environ.get('LINUX_BUILD_TEST_BASH') or shutil.which('bash')
        if not bash:
            self.skipTest('Bash is required for the actual source-selection function')
        function = re.search(r'(?ms)^do_fixedref\(\)\n\{\n.*?^\}',
                             (ROOT / 'update').read_text()).group()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / 'origin'
            origin.mkdir()
            git = ['git', '-c', 'commit.gpgsign=false', '-c', 'user.name=Linux build test',
                   '-c', 'user.email=linux-build@example.invalid']
            subprocess.run(git + ['init', '--quiet'], cwd=origin, check=True)
            (origin / 'version').write_text('first')
            subprocess.run(git + ['add', '.'], cwd=origin, check=True)
            subprocess.run(git + ['commit', '--quiet', '-m', 'first'], cwd=origin, check=True)
            first = subprocess.check_output(git + ['rev-parse', 'HEAD'], cwd=origin, text=True).strip()
            (origin / 'version').write_text('second')
            subprocess.run(git + ['commit', '--quiet', '-am', 'second'], cwd=origin, check=True)
            subprocess.run(git + ['clone', '--quiet', '--depth', '1', origin.as_uri(), 'work'], cwd=root, check=True)
            script = function + '\ndo_fixedref work "$1"\n'
            env = os.environ.copy()
            env['MPV_BUILD_FETCH_FIXED_REFS'] = '0'
            result = subprocess.run([bash, '-c', script, 'test', first], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            env['MPV_BUILD_FETCH_FIXED_REFS'] = '1'
            result = subprocess.run([bash, '-c', script, 'test', first], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'work/version').read_text(), 'first')
            branch = subprocess.check_output(git + ['branch', '--show-current'], cwd=origin, text=True).strip()
            (origin / 'version').write_text('third')
            subprocess.run(git + ['commit', '--quiet', '-am', 'third'], cwd=origin, check=True)
            result = subprocess.run([bash, '-c', script, 'test', branch], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'work/version').read_text(), 'third')


if __name__ == '__main__':
    unittest.main(verbosity=2)
