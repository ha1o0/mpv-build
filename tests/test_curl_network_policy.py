"""Apply the recipe patches to a copy and test their actual C policy functions.

Needs Python 3.8+, CMake, a C11 compiler and libcurl development files. Pass the
macOS curl install as --curl-prefix to test the shipped library. Does not build
mpv or modify its checkout. Applies the macOS patches in their build order.
"""
import argparse
import collections
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time


def run(args, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                            encoding="utf8", errors="replace", timeout=120, env=env)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def definition(source, pattern, semicolon=False):
    match = re.search(pattern, source)
    assert match, pattern
    brace = source.index("{", match.start())
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    if semicolon:
        assert source[end] == ";"
        end += 1
    return source[match.start():end] + "\n"


def patch_source(mpv_source, scratch):
    repo = Path(__file__).resolve().parents[1]
    source_dir = scratch / "source"
    source_files = ["stream/stream_curl.c", "stream/stream_curl.h",
                    "stream/network.c", "demux/demux_lavf.c", "meson.build"]
    for relative in source_files:
        target = source_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mpv_source / relative, target)
    run(["git", "init", "--quiet"], source_dir)
    spec = importlib.util.spec_from_file_location("linux_build", repo / "scripts/linux_build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    build.apply_series(source_dir, "mpv")
    return source_dir


def check_proxy_routing(binary):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            body = self.server.route.encode("ascii")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    servers = []
    try:
        for route in ["direct", "proxy"]:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.route = route
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        target_url = f"http://127.0.0.1:{servers[0].server_port}/probe"
        proxy_url = f"http://127.0.0.1:{servers[1].server_port}"
        env = os.environ.copy()
        for key in ["http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"]:
            env[key] = proxy_url
        for no_proxy, configured, expected in [
            ("", "environment", "proxy"),
            ("", "direct", "direct"),
            ("*", proxy_url, "proxy"),
            ("*", "environment", "direct"),
        ]:
            env["NO_PROXY"] = env["no_proxy"] = no_proxy
            actual = run([str(binary), target_url, configured], env=env).strip()
            assert actual == expected, (no_proxy, configured, actual)
        print("proxy routing: environment, explicit direct/proxy and NO_PROXY passed", flush=True)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv-source", type=Path, required=True)
    parser.add_argument("--curl-prefix", type=Path)
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--work-dir", type=Path,
                        help="optional parent directory for validation artifacts")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    # Default to the OS temporary directory; the supplied checkout is read only.
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="curl-policy-", dir=args.work_dir))
    original = (args.mpv_source / "stream/stream_curl.c").read_bytes()
    source_dir = patch_source(args.mpv_source, scratch)
    source = (source_dir / "stream/stream_curl.c").read_text(encoding="utf8")
    types = ""
    for name, kind in [("curl_proto", "enum"), ("curl_scheme", "struct"),
                       ("curl_opts", "struct"), ("curl_ctx", "struct"),
                       ("priv", "struct"), ("cmd_kind", "enum"), ("cmd", "struct")]:
        types += definition(source, rf"(?m)^{kind} {name} \{{", True)
    (scratch / "policy_types.inc").write_text(types, encoding="utf8")
    functions = ""
    for name in ["cancel_retry", "retry_request", "process_retries", "run_cmd",
                 "is_http_success", "ring_write", "write_callback", "xferinfo_callback",
                 "parse_content_range_total", "header_value", "finalize_probe",
                 "probe_http", "probe_ftp", "header_callback",
                 "is_recoverable_error", "start_request", "on_done"]:
        body = definition(source, rf"(?m)^static [\w *]+\b{name}\([^;]*?\)\n\{{")
        if name == "start_request":
            body = body.replace("start_request(", "start_request_native(", 1)
        functions += body
    (scratch / "policy_functions.inc").write_text(functions, encoding="utf8")
    # Check the production wiring omitted by the tiny harness probe/dispatcher.
    assert "p->last_activity = mp_time_sec();" in definition(source, r"(?m)^static size_t header_callback\(")
    assert "if (p->probed) {\n        p->stream_ok = true;\n        return;" in source
    assert "MPMIN(connect_timeout, p->net_opts->timeout)" in source
    assert "cancel_retry(c->p);" in source
    proxy_policy = definition(source, r"if \(p->net_opts->http_proxy\) \{")
    (scratch / "curl_proxy_policy.c").write_text("""
#include <stdio.h>
#include <string.h>
#include <curl/curl.h>
struct network_opts { const char *http_proxy; };
struct priv { struct network_opts *net_opts; };
static void configure_proxy(CURL *c, struct priv *p)
{
""" + proxy_policy + """
}
int main(int argc, char **argv)
{
    if (argc != 3) return 2;
    struct network_opts opts = {
        .http_proxy = !strcmp(argv[2], "environment") ? NULL :
                      !strcmp(argv[2], "direct") ? "" : argv[2]
    };
    struct priv p = { .net_opts = &opts };
    CURL *c = curl_easy_init();
    if (!c) return 3;
    curl_easy_setopt(c, CURLOPT_URL, argv[1]);
    curl_easy_setopt(c, CURLOPT_TIMEOUT_MS, 3000L);
    configure_proxy(c, &p);
    CURLcode result = curl_easy_perform(c);
    curl_easy_cleanup(c);
    return result == CURLE_OK ? 0 : 4;
}
""", encoding="utf8")
    cmake = """cmake_minimum_required(VERSION 3.21)
project(curl_policy_test C)
set(CMAKE_C_STANDARD 11)
if(WIN32)
 set(CURL_USE_STATIC_LIBS ON)
endif()
find_package(CURL REQUIRED)
add_executable(policy_test curl_network_policy.c)
add_executable(proxy_test curl_proxy_policy.c)
target_include_directories(policy_test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
target_link_libraries(policy_test PRIVATE CURL::libcurl)
target_link_libraries(proxy_test PRIVATE CURL::libcurl)
if(MSVC)
 target_compile_options(policy_test PRIVATE /experimental:c11atomics)
endif()
if(WIN32)
 target_link_libraries(policy_test PRIVATE wldap32 normaliz bcrypt advapi32 crypt32 secur32 ws2_32 iphlpapi)
 target_link_libraries(proxy_test PRIVATE wldap32 normaliz bcrypt advapi32 crypt32 secur32 ws2_32 iphlpapi)
endif()
"""
    (scratch / "CMakeLists.txt").write_text(cmake, encoding="utf8")
    shutil.copy2(repo / "tests/curl_network_policy.c", scratch)
    configure = [args.cmake, "-S", str(scratch), "-B", str(scratch / "build")]
    if args.curl_prefix:
        configure += ["-DCMAKE_PREFIX_PATH=" + str(args.curl_prefix.resolve())]
    run(configure)
    run([args.cmake, "--build", str(scratch / "build"), "--config", "Release"])
    binary = scratch / "build" / ("Release/policy_test.exe" if os.name == "nt" else "policy_test")
    print(run([str(binary)]).strip(), flush=True)
    proxy_binary = scratch / "build" / ("Release/proxy_test.exe" if os.name == "nt" else "proxy_test")
    check_proxy_routing(proxy_binary)
    requests = collections.defaultdict(list)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *unused):
            pass

        def do_GET(self):
            mode = self.path.strip("/")
            requests[mode].append(self.headers.get("Range"))
            attempt = len(requests[mode])
            try:
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mode == "forbidden" or (mode == "reject" and attempt > 1):
                    self.send_response(403)
                    self.send_header("Content-Length", "32")
                    self.end_headers()
                    self.wfile.write(b"x" * 32)
                    self.wfile.flush()
                    return
                if mode == "headers":
                    time.sleep(5)
                    return
                start = int((self.headers.get("Range") or "bytes=0-").split("=")[1].split("-")[0])
                size = 262144 if mode == "pause" else 96
                self.send_response(200 if mode == "streamed" else 206)
                if mode != "streamed":
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{size-1}/{size}")
                self.send_header("Content-Length", str(size - start))
                self.end_headers()
                if mode in ["stall", "cancel"]:
                    self.wfile.write(b"a" * 32)
                    self.wfile.flush()
                    time.sleep(5)
                elif mode in ["recover", "streamed"] and attempt == 1:
                    self.wfile.write(b"a" * 32)
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                elif mode == "reject" and attempt == 1:
                    self.wfile.write(b"a" * 32)
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self.wfile.write(b"b" * (size - start))
                    self.wfile.flush()
            except (OSError, BrokenPipeError):
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = {}
    try:
        for mode in ["recover", "streamed", "stall", "headers", "pause", "cancel",
                     "reject", "forbidden", "redirect"]:
            url = f"http://127.0.0.1:{server.server_port}/{mode}"
            result = json.loads(run([str(binary), url, mode]))
            result["ranges"] = requests[mode]
            results[mode] = result
            if mode == "recover":
                assert result["bytes"] == 96 and not result["error"], result
                assert result["ranges"] == ["bytes=0-", "bytes=32-"], result
            elif mode == "streamed":
                assert result["bytes"] == 128 and not result["error"], result
                assert result["ranges"] == ["bytes=0-", None], result
            elif mode in ["stall", "headers"]:
                assert result["error"] and result["attempts"] == 2 and result["elapsed"] < 9, result
            elif mode == "pause":
                assert not result["error"] and result["attempts"] == 1 and result["pauses"] > 0, result
                assert result["bytes"] == 262144 and result["elapsed"] >= 3, result
            elif mode == "cancel":
                assert result["cancelled"] and result["attempts"] == 1 and result["elapsed"] < 3, result
            elif mode == "reject":
                assert result["error"] and result["attempts"] == 2 and result["bytes"] == 32, result
            elif mode == "forbidden":
                assert result["error"] and result["attempts"] == 1 and result["bytes"] == 0, result
            elif mode == "redirect":
                assert not result["error"] and result["attempts"] == 1 and result["bytes"] == 96, result
            print(mode + ": " + json.dumps(result), flush=True)
    finally:
        server.shutdown()
        server.server_close()
    assert (args.mpv_source / "stream/stream_curl.c").read_bytes() == original
    (scratch / "results.json").write_text(json.dumps(results, indent=2), encoding="utf8")
    print("Passed; artifacts: " + str(scratch), flush=True)


if __name__ == "__main__":
    main()
