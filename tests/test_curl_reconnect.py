#!/usr/bin/env python3
"""Test the real patched per-request FFmpeg reconnect policy with a fake clock.

Requires Python 3.9+, Git, a C11 compiler, libcurl development files and the
host-compatible FFmpeg prefix from this build. No network server is contacted.
The tests compile the production parser, argument transfer and retry functions.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from test_curl_network_policy import definition, patch_source


def run(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                            timeout=60)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv-source", type=Path, required=True)
    parser.add_argument("--ffmpeg-prefix", type=Path, required=True)
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="linux-curl-reconnect-", dir=args.work_dir))
    source_dir = patch_source(args.mpv_source, scratch)
    source = (source_dir / "stream/stream_curl.c").read_text()
    # Reuse only the existing small mpv type/clock shims, not its test cases.
    shims = (repo / "tests/curl_network_policy.c").read_text().split(
        '#include "policy_types.inc"', 1)[0]
    types = ""
    for name, kind in (("curl_proto", "enum"), ("curl_scheme", "struct"),
                       ("curl_opts", "struct"), ("curl_ctx", "struct"),
                       ("priv", "struct")):
        types += definition(source, rf"(?m)^{kind} {name} \{{", True)
    parser_source = source[source.index("struct curl_reconnect_opts {"):
                           source.index("// Special args for use by lavf.")]
    parser_source += definition(source, r"(?m)^struct curl_open_args \{", True)
    functions = ""
    for name in ("cancel_retry", "retry_request", "process_retries",
                 "is_recoverable_error", "start_request", "on_done"):
        functions += definition(source, rf"(?m)^static [\w *]+\b{name}\([^;]*?\)\n\{{")
    curl_open = definition(source, r"(?m)^static int curl_open\(")
    transfer = definition(curl_open, r"if \(args->special_arg\) \{")
    transport = definition(source, r"(?m)^static int open_curl_transport\(")
    consume = definition(transport, r"if \(oa.reconnect.set\) \{")
    guard = transport[:transport.index("    // The context is required")]
    guard = guard.replace("open_curl_transport(", "guard_transport(", 1) + "    return 0;\n}\n"
    # These cover the omitted allocation path, including no early consumption
    # on disabled/unsupported URLs or unsuccessful stream construction.
    assert transport.index("if (!enabled)") < transport.index("read_lavf_reconnect(")
    assert transport.index("*cookie_out = c;") < transport.index("if (oa.reconnect.set)")
    assert "if (cs->proto == MP_CURL_PROTO_HTTP)" in transport
    assert "p->request_has_response = true;" in definition(source, r"(?m)^static void probe_http\(")
    code = shims + r'''
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <libavformat/avio.h>
#include <libavutil/error.h>
#include <libavutil/opt.h>
#include <libavutil/dict.h>
#include <libavutil/version.h>
#define mp_assert assert
#define mp_require assert
''' + types + parser_source + r'''
static void start_request(struct priv *p);
static void log_curl_error(struct priv *p, const char *what, CURLcode code) {}
static CURLMcode record_add(CURLM *multi, CURL *easy) { starts++; return CURLM_OK; }
#define curl_multi_add_handle record_add
''' + functions + r'''
#undef curl_multi_add_handle
struct stream_open_args { const void *special_arg; };
static void apply_args(struct priv *p, const struct stream_open_args *args)
''' + "{\n" + transfer + "}\n" + r'''
static void consume_args(AVDictionary **options, struct curl_open_args oa)
''' + "{\n" + consume + "}\n" + r'''
struct mpv_global { struct curl_opts opts; };
struct demuxer { struct mpv_global *global; };
static int curl_conf;
static bool supported = true, available = true;
static const struct curl_scheme *curl_scheme_lookup(bstr url)
{
    static struct curl_scheme scheme = {.proto = MP_CURL_PROTO_HTTP};
    return supported ? &scheme : NULL;
}
static bool curl_has_proto(bstr scheme) { return available; }
static void *mp_get_config_group(void *parent, struct mpv_global *global, void *conf)
{
    struct curl_opts *opts = malloc(sizeof(*opts));
    *opts = global->opts;
    return opts;
}
#define talloc_free free
''' + guard + r'''
struct fixture {
    struct priv p;
    struct curl_ctx ctx;
    struct curl_opts opts;
    struct mp_network_opts net;
    struct curl_scheme scheme;
};
static void init(struct fixture *f)
{
    memset(f, 0, sizeof(*f));
    f->p.ctx = &f->ctx;
    f->p.opts = &f->opts;
    f->p.net_opts = &f->net;
    f->p.scheme = &f->scheme;
    f->p.content_size = -1;
    f->p.curl = curl_easy_init();
    assert(f->p.curl);
    f->scheme.proto = MP_CURL_PROTO_HTTP;
    f->opts.max_retries = 5;
    f->opts.retry_delay_max = 5;
    f->opts.reconnect_streamed = true;
    now = 0;
    starts = 0;
}
static void finish(struct fixture *f) { curl_easy_cleanup(f->p.curl); }
static void inherit(struct fixture *f, const char *values)
{
    AVDictionary *dict = NULL;
    assert(av_dict_parse_string(&dict, values, "=", ",", 0) == 0);
    struct curl_open_args oa = {0};
    assert(read_lavf_reconnect(dict, &oa.reconnect) == 0);
    struct stream_open_args args = {.special_arg = &oa};
    apply_args(&f->p, &args);
    av_dict_free(&dict);
}
static void body(struct fixture *f)
{
    f->p.probed = true;
    f->p.request_has_response = true;
}
static void no_retry(struct fixture *f, CURLcode code)
{
    on_done(&f->p, code);
    assert(starts == 0 && !f->ctx.retries && f->p.stream_error);
}
int main(void)
{
    assert(curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK);
    struct curl_open_args oa = {0};
    AVDictionary *dict = NULL;
    assert(read_lavf_reconnect(NULL, &oa.reconnect) == 0);
    assert(!oa.reconnect.set && oa.reconnect.reconnect_delay_max == 120);
    assert(av_dict_parse_string(&dict,
        "reconnect=yes,reconnect_streamed=true,reconnect_on_network_error=1,reconnect_delay_max=5,offset=42",
        "=", ",", 0) == 0);
    assert(read_lavf_reconnect(dict, &oa.reconnect) == 0);
    assert(oa.reconnect.set && oa.reconnect.reconnect && oa.reconnect.reconnect_streamed);
    assert(oa.reconnect.reconnect_on_network_error && oa.reconnect.reconnect_delay_max == 5);
    assert(av_dict_count(dict) == 5); // parsing alone must not consume fallback options
    consume_args(&dict, oa);
    assert(av_dict_count(dict) == 1 && av_dict_get(dict, "offset", NULL, 0));
    av_dict_free(&dict);
    const char *invalid[] = {"reconnect=2", "reconnect=nonsense", "reconnect_delay_max=-1",
                            "reconnect_delay_max=4295", "reconnect_delay_max=nan"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(*invalid); i++) {
        assert(av_dict_parse_string(&dict, invalid[i], "=", ",", 0) == 0);
        assert(read_lavf_reconnect(dict, &oa.reconnect) < 0);
        assert(av_dict_count(dict) == 1);
        av_dict_free(&dict);
    }
    // Execute the production early guards with an intact fallback dictionary.
    struct mpv_global global = {.opts.enabled = false};
    struct demuxer demuxer = {.global = &global};
    AVIOContext *pb = NULL;
    void *cookie = NULL;
    av_dict_set(&dict, "reconnect_streamed", "1", 0);
    assert(guard_transport(&demuxer, &pb, &cookie, "http://fixture", 0, &dict, NULL, NULL) == AVERROR(ENOSYS));
    global.opts.enabled = true;
    supported = false;
    assert(guard_transport(&demuxer, &pb, &cookie, "unsupported://fixture", 0, &dict, NULL, NULL) == AVERROR(ENOSYS));
    supported = true;
    available = false;
    assert(guard_transport(&demuxer, &pb, &cookie, "http://fixture", 0, &dict, NULL, NULL) == AVERROR(ENOSYS));
    available = true;
    assert(guard_transport(&demuxer, &pb, &cookie, "http://fixture", AVIO_FLAG_WRITE, &dict, NULL, NULL) == AVERROR(ENOSYS));
    assert(av_dict_count(dict) == 1);
    av_dict_free(&dict);
    struct fixture f;
    init(&f);
    inherit(&f, "offset=42");
    assert(!f.p.lavf_reconnect_set);
    on_done(&f.p, CURLE_COULDNT_CONNECT);
    assert(starts == 0 && f.p.retry_at == 1 && f.p.retry_queued); // original curl cap
    finish(&f);
    const char *disabled[] = {"reconnect=0", "reconnect_streamed=1", "reconnect_delay_max=5"};
    for (size_t i = 0; i < sizeof(disabled) / sizeof(*disabled); i++) {
        init(&f); inherit(&f, disabled[i]); no_retry(&f, CURLE_COULDNT_CONNECT); finish(&f);
        init(&f); inherit(&f, disabled[i]); body(&f); f.p.seekable = true;
        no_retry(&f, CURLE_PARTIAL_FILE); finish(&f);
    }
    init(&f); inherit(&f, "reconnect=1");
    no_retry(&f, CURLE_COULDNT_CONNECT); finish(&f);
    init(&f); inherit(&f, "reconnect=1"); body(&f);
    no_retry(&f, CURLE_PARTIAL_FILE); finish(&f); // nonseekable still needs streamed
    init(&f); inherit(&f, "reconnect=1"); body(&f); f.p.seekable = true;
    on_done(&f.p, CURLE_PARTIAL_FILE); assert(starts == 1); finish(&f);
    init(&f); inherit(&f, "reconnect_on_network_error=1"); body(&f);
    no_retry(&f, CURLE_RECV_ERROR); finish(&f); // network flag cannot authorize body retries
    init(&f); inherit(&f, "reconnect=1,reconnect_streamed=1"); body(&f);
    on_done(&f.p, CURLE_PARTIAL_FILE);
    assert(starts == 1 && !f.p.request_has_response && f.p.probed);
    on_done(&f.p, CURLE_COULDNT_CONNECT); // later retry fails during connect
    assert(starts == 1 && f.p.stream_error && !f.ctx.retries); finish(&f);
    init(&f);
    inherit(&f, "reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=5");
    on_done(&f.p, CURLE_COULDNT_CONNECT); assert(starts == 1 && !f.ctx.retries);
    on_done(&f.p, CURLE_COULDNT_CONNECT); assert(f.p.retry_at == 1 && f.ctx.retries);
    now = 1; process_retries(&f.ctx); assert(starts == 2);
    on_done(&f.p, CURLE_COULDNT_CONNECT); assert(f.p.retry_at == 4);
    now = 4; process_retries(&f.ctx); assert(starts == 3);
    on_done(&f.p, CURLE_COULDNT_CONNECT);
    assert(starts == 3 && f.p.stream_error && !f.ctx.retries); finish(&f);
    init(&f); inherit(&f, "reconnect_on_network_error=1,reconnect_delay_max=0");
    on_done(&f.p, CURLE_COULDNT_CONNECT); assert(starts == 1);
    on_done(&f.p, CURLE_COULDNT_CONNECT); assert(starts == 1 && f.p.stream_error); finish(&f);
    init(&f); inherit(&f, "reconnect_on_network_error=1"); f.opts.max_retries = 0;
    no_retry(&f, CURLE_COULDNT_CONNECT); finish(&f);
    init(&f); inherit(&f, "reconnect_on_network_error=1");
    on_done(&f.p, CURLE_COULDNT_CONNECT); on_done(&f.p, CURLE_COULDNT_CONNECT);
    assert(f.ctx.retries && starts == 1);
    atomic_store(&f.p.aborted, true);
    process_retries(&f.ctx);
    assert(!f.ctx.retries && starts == 1); finish(&f); // cancelled delay never starts again
    init(&f); inherit(&f, "reconnect_on_network_error=1");
    on_done(&f.p, CURLE_COULDNT_CONNECT); on_done(&f.p, CURLE_COULDNT_CONNECT);
    cancel_retry(&f.p); now = 999; process_retries(&f.ctx);
    assert(!f.ctx.retries && starts == 1); finish(&f);
    // Received data opens a fresh FFmpeg retry budget; the old curl branch stays unchanged.
    init(&f); inherit(&f, "reconnect=1,reconnect_streamed=1,reconnect_delay_max=5"); body(&f);
    f.p.retry_count = 5; f.p.request_received = 188;
    on_done(&f.p, CURLE_PARTIAL_FILE);
    assert(starts == 1 && f.p.retry_count == 1 && f.p.request_start == 0); finish(&f);
    init(&f); body(&f); f.p.retry_count = 5; f.p.request_received = 188;
    no_retry(&f, CURLE_PARTIAL_FILE); assert(f.p.retry_count == 5); finish(&f);
    CURLcode forbidden[] = {CURLE_HTTP_RETURNED_ERROR, CURLE_WRITE_ERROR,
                           CURLE_PEER_FAILED_VERIFICATION, CURLE_SSL_CACERT_BADFILE};
    for (size_t i = 0; i < sizeof(forbidden) / sizeof(*forbidden); i++) {
        init(&f); inherit(&f, "reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1");
        body(&f); no_retry(&f, forbidden[i]); finish(&f);
    }
    init(&f); inherit(&f, "reconnect_on_network_error=1");
    on_done(&f.p, CURLE_SSL_CONNECT_ERROR); assert(starts == 1); finish(&f);
    init(&f); inherit(&f, "reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1"); body(&f);
    on_done(&f.p, CURLE_OK); assert(f.p.stream_eof && starts == 0); finish(&f);
    curl_global_cleanup();
    puts("PASS: dictionary parsing/consumption, fallback guards, request policy precedence, connect/read gates,");
    puts("      0/1/3 give-up delay, max-retries, cancellation, progress reset, HTTP/TLS exclusions and EOF");
    return 0;
}
'''
    test = scratch / "reconnect.c"
    test.write_text(code)
    prefix = args.ffmpeg_prefix.resolve()
    binary = scratch / "reconnect"
    env = os.environ.copy()
    env['PKG_CONFIG_PATH'] = str(prefix / 'lib/pkgconfig') + os.pathsep + env.get('PKG_CONFIG_PATH', '')
    # FFmpeg is static in this build. Include its private math/thread/link
    # dependencies; unlike macOS, Linux does not fold libm into libc.
    flags = []
    for options in (['--static', '--cflags', '--libs', 'libavutil'],
                    ['--cflags', '--libs', 'libcurl']):
        flags += shlex.split(subprocess.check_output(['pkg-config'] + options, env=env, text=True))
    run([args.cc, "-std=c11", "-O0", "-g", str(test),
         "-Wl,-rpath," + str(prefix / "lib"), *flags, "-o", str(binary)])
    print(run([str(binary)]), end="")
    print("Artifacts: " + str(scratch))


if __name__ == "__main__":
    main()
