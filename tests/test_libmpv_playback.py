#!/usr/bin/env python3
"""Exercise real libmpv playback and teardown in isolated child processes.

Pass the built libmpv shared library with --library, and
a directory containing two one-second MPEG-TS files, seg0.ts and seg1.ts, with
--fixture. No external server, Python packages, display, or audio device is used.
The encrypted HLS case uses the openssl executable to encrypt that same fixture.
Each case preserves mpv logs and teardown checkpoints even if libmpv aborts.
"""

import argparse
import ctypes as C
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import urlsplit


CASES = ("hls-eof", "hls-open-failure", "hls-cancel", "hls-reconnect", "hls-encrypted",
         "local-ts", "http-ts", "local-mp4", "http-mp4", "local-mkv", "http-mkv")
MODES = ("yes", "no", "auto")


class Event(C.Structure):
    _fields_ = [("event_id", C.c_int), ("error", C.c_int),
                ("reply_userdata", C.c_uint64), ("data", C.c_void_p)]


class Log(C.Structure):
    _fields_ = [("prefix", C.c_char_p), ("level", C.c_char_p),
                ("text", C.c_char_p), ("log_level", C.c_int)]


class EndFile(C.Structure):
    _fields_ = [("reason", C.c_int), ("error", C.c_int)]


def playlist(*segments):
    return ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n" +
            "".join("#EXTINF:1.000000,\n" + name + "\n" for name in segments) +
            "#EXT-X-ENDLIST\n").encode()


def serve_fixture(fixture, requests, blocked, release, truncated, encrypted=False):
    payloads = {
        "/index.m3u8": playlist("seg0.ts", "seg1.ts"),
        "/missing.m3u8": playlist("missing.ts"),
        "/blocked.m3u8": playlist("blocked.ts"),
        "/retry.m3u8": playlist("retry.ts", "seg1.ts"),
        "/retry.ts": (fixture / "seg0.ts").read_bytes(),
        "/seg0.ts": (fixture / "seg0.ts").read_bytes(),
        "/seg1.ts": (fixture / "seg1.ts").read_bytes(),
    }
    for name in ('video.mp4', 'video.mkv'):
        if (fixture / name).exists():
            payloads['/' + name] = (fixture / name).read_bytes()
    if encrypted:
        # Fixed test key/IV; encrypt each MPEG-TS segment independently, with
        # PKCS#7 padding just like an AES-128 HLS producer.
        key, iv = bytes(range(16)), bytes(range(16, 32))
        payloads["/key.bin"] = key
        key_tag = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x{}\n'.format(iv.hex())
        payloads["/encrypted.m3u8"] = playlist(
            "encrypted-seg0.ts", "encrypted-seg1.ts").replace(
                b"#EXTINF:", key_tag.encode() + b"#EXTINF:", 1)
        for index in range(2):
            result = subprocess.run(
                ["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
                input=payloads["/seg{}.ts".format(index)], capture_output=True, timeout=10)
            if result.returncode:
                raise RuntimeError("cannot encrypt HLS fixture: " +
                                   result.stderr.decode("utf-8", errors="replace"))
            payloads["/encrypted-seg{}.ts".format(index)] = result.stdout

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append(path)
            if path == "/blocked.ts":
                data = payloads["/seg0.ts"]
            else:
                data = payloads.get(path)
            if data is None:
                self.send_error(404)
                return
            offset = 0
            ranged = path in ('/video.mp4', '/video.mkv') and self.headers.get('Range')
            if ranged:
                offset = int(ranged.removeprefix('bytes=').split('-')[0])
                if offset >= len(data):
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */{}'.format(len(data)))
                    self.end_headers()
                    return
            self.send_response(206 if ranged else 200)
            if path in ('/video.mp4', '/video.mkv'):
                self.send_header('Accept-Ranges', 'bytes')
            if ranged:
                self.send_header('Content-Range', 'bytes {}-{}/{}'.format(offset, len(data) - 1, len(data)))
            data = data[offset:]
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "application/vnd.apple.mpegurl"
                             if path.endswith(".m3u8") else "video/mp2t")
            self.end_headers()
            try:
                if path == "/blocked.ts":
                    # Keep nested segment I/O open until mpv has cancelled it.
                    self.wfile.write(data[:188])
                    self.wfile.flush()
                    blocked.set()
                    release.wait()
                    self.wfile.write(data[188:])
                elif path == "/retry.ts" and requests.count(path) == 1:
                    # Advertise the complete segment, then disconnect early.
                    # The application's lavf reconnect options must also work
                    # when this nested HTTP request is handled by curl.
                    self.wfile.write(data[:188])
                    self.wfile.flush()
                    truncated.set()
                    self.close_connection = True
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def load_library(path):
    lib = C.CDLL(str(path))
    signatures = {
        "mpv_create": ([], C.c_void_p),
        "mpv_set_option_string": ([C.c_void_p, C.c_char_p, C.c_char_p], C.c_int),
        "mpv_request_log_messages": ([C.c_void_p, C.c_char_p], C.c_int),
        "mpv_initialize": ([C.c_void_p], C.c_int),
        "mpv_command": ([C.c_void_p, C.POINTER(C.c_char_p)], C.c_int),
        "mpv_wait_event": ([C.c_void_p, C.c_double], C.POINTER(Event)),
        "mpv_get_property_string": ([C.c_void_p, C.c_char_p], C.c_void_p),
        "mpv_free": ([C.c_void_p], None),
        "mpv_terminate_destroy": ([C.c_void_p], None),
    }
    for name, (arguments, result) in signatures.items():
        getattr(lib, name).argtypes = arguments
        getattr(lib, name).restype = result
    return lib


def check(result, label):
    if result < 0:
        raise RuntimeError("{} returned {}".format(label, result))


def property_string(lib, handle, name):
    value = lib.mpv_get_property_string(handle, name.encode())
    if not value:
        return None
    try:
        return C.string_at(value).decode("utf-8", errors="replace")
    finally:
        lib.mpv_free(value)


def command(lib, handle, *arguments):
    argv = (C.c_char_p * (len(arguments) + 1))(
        *[arg.encode() for arg in arguments], None)
    check(lib.mpv_command(handle, argv), arguments[0])


def child(args):
    case, mode = args.case[0], args.curl[0]
    indexed_media = case.endswith(('-mp4', '-mkv'))
    summary = {"case": case, "curl": mode, "loaded": False, "end": None,
               "curl_requests": 0, "stop_sent": False, "requests": [],
               "errors": [], "teardown_complete": False}

    def checkpoint(phase):
        summary["phase"] = phase
        pending = args.output_dir / "result.json.tmp"
        pending.write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        pending.replace(args.output_dir / "result.json")
        print(json.dumps({"phase": phase, "case": case, "curl": mode}), flush=True)

    checkpoint("starting")
    blocked, release = threading.Event(), threading.Event()
    truncated = threading.Event()
    server, lib, handle = None, None, None
    log = (args.output_dir / "mpv.log").open("w", encoding="utf-8", buffering=1)
    try:
        server = serve_fixture(args.fixture, summary["requests"], blocked, release,
                               truncated, encrypted=case == "hls-encrypted")
        lib = load_library(args.library)
        handle = lib.mpv_create()
        if not handle:
            raise RuntimeError("mpv_create failed")
        options = {"vo": "null", "ao": "null", "hwdec": "no", "audio": "no",
                   "terminal": "no", "idle": "yes", "cache": "no",
                   "config": "no", "http-proxy": "",
                   "network-timeout": "5", "msg-level": "all=trace"}
        if indexed_media:
            options.pop('audio')
            options['pause'] = 'yes'
        if case.endswith('-mkv'):
            options.update({'demuxer-mkv-skip-tags': 'yes', 'demuxer-mkv-defer-cues': 'yes'})
        if mode != "auto":
            options["curl-enabled"] = mode
        if case == "hls-reconnect":
            options["demuxer-lavf-o"] = (
                "reconnect=1,reconnect_streamed=1,"
                "reconnect_on_network_error=1,reconnect_delay_max=5")
        for name, value in options.items():
            check(lib.mpv_set_option_string(handle, name.encode(), value.encode()), name)
        check(lib.mpv_request_log_messages(handle, b"trace"), "request logs")
        check(lib.mpv_initialize(handle), "initialize")
        summary["versions"] = {
            key: property_string(lib, handle, key)
            for key in ("mpv-version", "ffmpeg-version", "options/curl-enabled")}
        checkpoint("initialized")
        paths = {"hls-eof": "index.m3u8", "hls-open-failure": "missing.m3u8",
                 "hls-cancel": "blocked.m3u8", "hls-reconnect": "retry.m3u8",
                 "hls-encrypted": "encrypted.m3u8",
                 "http-ts": "seg0.ts", "local-ts": "seg0.ts",
                 "local-mp4": "video.mp4", "http-mp4": "video.mp4",
                 "local-mkv": "video.mkv", "http-mkv": "video.mkv"}
        url = (str(args.fixture / paths[case]) if case.startswith('local-') else
               "http://127.0.0.1:{}/{}".format(server.server_port, paths[case]))
        command(lib, handle, "loadfile", url, "replace")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if case == "hls-cancel" and blocked.is_set() and not summary["stop_sent"]:
                summary["stop_sent"] = True
                checkpoint("cancelling-blocked-segment")
                command(lib, handle, "stop")
            event = lib.mpv_wait_event(handle, 0.05).contents
            if event.event_id == 2:
                entry = C.cast(event.data, C.POINTER(Log)).contents
                line = "[{}] {}".format(entry.prefix.decode("utf-8", errors="replace"),
                                        entry.text.decode("utf-8", errors="replace"))
                log.write(line)
                summary["curl_requests"] += "starting curl request" in line
            elif event.event_id == 8:
                summary["loaded"] = True
                checkpoint("file-loaded")
                if indexed_media:
                    if property_string(lib, handle, 'pause') != 'yes':
                        raise RuntimeError('initial explicit pause was lost')
                    command(lib, handle, 'seek', '0.6', 'absolute+exact')
                    summary['seek_requested'] = True
            elif event.event_id == 7:
                ending = C.cast(event.data, C.POINTER(EndFile)).contents
                summary["end"] = {"reason": ending.reason, "error": ending.error}
                checkpoint("end-file")
                if ending.reason != 5:
                    break
            if indexed_media and summary.get('seek_requested') and not summary.get('seek_completed'):
                position = property_string(lib, handle, 'time-pos')
                if position and float(position) >= 0.5 and property_string(lib, handle, 'seeking') == 'no':
                    summary['seek_completed'] = True
                    command(lib, handle, 'set', 'pause', 'no')
        else:
            raise RuntimeError("no final END_FILE event within {} seconds".format(args.timeout))

        ending = summary["end"]
        if case == "hls-open-failure":
            if summary["loaded"] or ending["reason"] != 4 or ending["error"] >= 0:
                raise RuntimeError("missing HLS segment did not fail to open: {}".format(ending))
        elif case == "hls-cancel":
            if not summary["stop_sent"] or ending["reason"] != 2:
                raise RuntimeError("blocked HLS segment did not stop cleanly: {}".format(ending))
        elif not summary["loaded"] or ending["reason"] != 0:
            raise RuntimeError("playback did not reach normal EOF: {}".format(ending))
        if indexed_media and not summary.get('seek_completed'):
            raise RuntimeError('paused seek and resume did not complete')

        # ERROR is the only reason for which libmpv defines the error field.
        # Assert the requests too: an early playlist error must not masquerade
        # as the intended nested-segment failure, or a partial HLS EOF as success.
        required_paths = {
            "hls-eof": {"/index.m3u8", "/seg0.ts", "/seg1.ts"},
            "hls-open-failure": {"/missing.m3u8", "/missing.ts"},
            "hls-cancel": {"/blocked.m3u8", "/blocked.ts"},
            "hls-reconnect": {"/retry.m3u8", "/retry.ts", "/seg1.ts"},
            "hls-encrypted": {"/encrypted.m3u8", "/key.bin",
                              "/encrypted-seg0.ts", "/encrypted-seg1.ts"},
            "http-ts": {"/seg0.ts"},
            "local-ts": set(),
            "local-mp4": set(), "http-mp4": {'/video.mp4'},
            "local-mkv": set(), "http-mkv": {'/video.mkv'},
        }
        if not required_paths[case].issubset(summary["requests"]):
            raise RuntimeError("expected fixture paths were not requested")
        if case == "hls-reconnect":
            summary["truncated_segment"] = truncated.is_set()
            if not truncated.is_set() or summary["requests"].count("/retry.ts") < 2:
                raise RuntimeError("truncated nested segment was not retried")
        enabled = mode != "no"
        if summary["versions"]["options/curl-enabled"] != ("yes" if enabled else "no"):
            raise RuntimeError("unexpected curl-enabled option/default")
        if bool(summary["curl_requests"]) != (enabled and not case.startswith('local-')):
            raise RuntimeError("unexpected curl transport use")
        checkpoint("validated")
    except Exception as error:
        summary["errors"].append(str(error))
        traceback.print_exc()
    finally:
        # Persist evidence before destruction: the regression aborts in native
        # code and cannot be caught by Python's exception handler.
        checkpoint("teardown-started")
        if handle:
            lib.mpv_terminate_destroy(handle)
        summary["teardown_complete"] = True
        release.set()
        if server:
            server.shutdown()
            server.server_close()
        log.close()
        checkpoint("complete")
    return int(bool(summary["errors"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--curl", choices=MODES, action="append",
                        help="auto leaves the library's default unchanged (expected: yes)")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.library, args.fixture = args.library.resolve(), args.fixture.resolve()
    if not args.library.is_file():
        parser.error("--library must be a host-compatible libmpv binary")
    for name in ("seg0.ts", "seg1.ts"):
        path = args.fixture / name
        if not path.is_file() or path.stat().st_size <= 188:
            parser.error("--fixture must contain MPEG-TS files seg0.ts and seg1.ts")
    for case in args.case or CASES:
        if case.endswith(('-mp4', '-mkv')) and not (args.fixture / ('video.' + case.rsplit('-', 1)[1])).is_file():
            parser.error('--fixture must contain video.mp4 and video.mkv for indexed media cases')
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.child:
        return child(args)

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="libmpv-curl-playback-", dir=args.output_dir))
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update({"NO_PROXY": "*", "no_proxy": "*"})
    framework = next((path for path in args.library.parents
                      if path.suffix == ".framework"), None)
    if framework:
        # libmpv uses @rpath for sibling frameworks. Python is not an app bundle
        # with that runpath, so supply the matching build's directory at launch.
        env["DYLD_FRAMEWORK_PATH"] = str(framework.parent)
    elif sys.platform == "darwin":
        env["DYLD_LIBRARY_PATH"] = str(args.library.parent)
    else:
        env["LD_LIBRARY_PATH"] = str(args.library.parent) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    results = []
    for case in dict.fromkeys(args.case or CASES):
        for mode in dict.fromkeys(args.curl or MODES):
            if not case.startswith("hls-") and mode == "auto" and not args.curl:
                continue
            case_dir = output / (case + "-" + mode)
            case_dir.mkdir()
            cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "--child",
                   "--library", str(args.library), "--fixture", str(args.fixture),
                   "--case", case, "--curl", mode, "--output-dir", str(case_dir),
                   "--timeout", str(args.timeout)]
            with (case_dir / "stdout.log").open("w") as stdout, \
                    (case_dir / "stderr.log").open("w") as stderr:
                try:
                    code = subprocess.run(cmd, env=env, stdout=stdout, stderr=stderr,
                                          timeout=args.timeout + 10).returncode
                    failure = (signal.Signals(-code).name if code < 0 else
                               "exit {}".format(code) if code else None)
                except subprocess.TimeoutExpired:
                    code, failure = None, "child timed out"
            result_file = case_dir / "result.json"
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                result = {"case": case, "curl": mode,
                          "errors": ["cannot read child result: {}".format(error)]}
            result.update({"returncode": code, "failure": failure,
                           "artifacts": str(case_dir)})
            result["passed"] = (code == 0 and result.get("teardown_complete", False)
                                and not result.get("errors"))
            results.append(result)
            print("{} {} curl={}: {}".format(
                "PASS" if result["passed"] else "FAIL", case, mode,
                "; ".join(([failure] if failure else []) + result.get("errors", []))
                or result.get("phase")),
                flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n",
                                         encoding="utf-8")
    print("Artifacts: " + str(output))
    return int(any(not result["passed"] for result in results))


if __name__ == "__main__":
    sys.exit(main())
