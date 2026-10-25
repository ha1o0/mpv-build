// Tests the actual functions extracted from our patched stream_curl.c.
// The small mpv shims isolate policy from demuxing/rendering; libcurl is real.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <curl/curl.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <time.h>
#endif

#define MPMIN(a, b) ((a) < (b) ? (a) : (b))
#define MP_TRACE(...) ((void)0)
#define MP_WARN(...) ((void)0)
#define MP_DBG(...) ((void)0)
#define MP_ERR(...) ((void)0)
#define MSGL_DEBUG 1
#ifdef _WIN32
#define strcasecmp _stricmp
#endif
typedef int mp_thread;
typedef int mp_mutex;
typedef int mp_cond;
typedef struct bstr { const char *start; size_t len; } bstr;
struct mp_log;
static bool mp_msg_test(struct mp_log *log, int level) { return false; }
static bstr bstr0(const char *s) { return (bstr){s, strlen(s)}; }
static bool bstr_startswith0(bstr s, const char *prefix)
{
    size_t n = strlen(prefix);
    return s.len >= n && !memcmp(s.start, prefix, n);
}
static bool bstr_equals0(bstr s, const char *text)
{
    return s.len == strlen(text) && !memcmp(s.start, text, s.len);
}
static bstr bstr_strip_linebreaks(bstr s)
{
    while (s.len && (s.start[s.len - 1] == '\r' || s.start[s.len - 1] == '\n')) s.len--;
    return s;
}
static bool bstr_split_tok(bstr s, const char *tok, bstr *a, bstr *b)
{
    assert(strlen(tok) == 1);
    const char *split = memchr(s.start, tok[0], s.len);
    if (!split) return false;
    *a = (bstr){s.start, split - s.start};
    *b = (bstr){split + 1, s.len - a->len - 1};
    return true;
}
static long long bstrtoll(bstr s, bstr *rest, int base)
{
    char text[128];
    assert(s.len < sizeof(text));
    memcpy(text, s.start, s.len);
    text[s.len] = 0;
    char *end;
    long long value = strtoll(text, &end, base);
    *rest = (bstr){s.start + (end - text), s.len - (end - text)};
    return value;
}
static bool mp_icy_active(void *icy) { return false; }
static void mp_icy_reset(void *icy) {}
static void mp_icy_add_header(void *icy, bstr line) {}
struct mp_network_opts { double timeout; };
static void mp_mutex_lock(mp_mutex *m) { (void)m; }
static void mp_mutex_unlock(mp_mutex *m) { (void)m; }
static void mp_cond_broadcast(mp_cond *c) { (*c)++; }
static bool network;
static double now;
static int starts, pauses;
static double mp_time_sec(void)
{
    if (!network) return now;
#ifdef _WIN32
    return GetTickCount64() / 1000.0;
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
#endif
}
static void mp_icy_process(void *icy, const char *data, size_t len,
                          void (*write)(void *, const char *, size_t), void *ctx)
{
    (void)icy;
    write(ctx, data, len);
}

#include "policy_types.inc"
static void start_request_native(struct priv *p);
static void start_request(struct priv *p)
{
    starts++;
    if (network) {
        start_request_native(p);
    } else {
        p->read_timed_out = false;
        p->last_activity = mp_time_sec();
    }
}
static void on_done(struct priv *p, CURLcode code);
static void log_curl_error(struct priv *p, const char *what, CURLcode code)
{
    (void)p; (void)what; (void)code;
}
static CURLcode policy_pause(CURL *handle, int flags)
{
    return network ? curl_easy_pause(handle, flags) : CURLE_OK;
}
#define curl_easy_pause policy_pause
#include "policy_functions.inc"
#undef curl_easy_pause

struct fixture {
    struct priv p;
    struct curl_ctx ctx;
    struct curl_opts opts;
    struct mp_network_opts net;
    struct curl_scheme scheme;
    uint8_t buffer[65536];
};
static void init(struct fixture *f)
{
    memset(f, 0, sizeof(*f));
    f->p.ctx = &f->ctx;
    f->p.opts = &f->opts;
    f->p.net_opts = &f->net;
    f->p.scheme = &f->scheme;
    f->p.buffer = f->buffer;
    f->p.buffer_size = sizeof(f->buffer);
    f->p.content_size = -1;
    f->scheme.proto = MP_CURL_PROTO_HTTP;
    f->net.timeout = 1;
    f->opts.max_retries = 2;
    f->opts.retry_delay_max = 5;
    f->opts.reconnect_streamed = true;
    starts = 0;
    now = 0;
}
static void unit_tests(void)
{
    struct fixture f;
    init(&f);
    now = 1.01;
    assert(xferinfo_callback(&f.p, 0, 0, 0, 0) == 1);
    assert(f.p.read_timed_out);
    on_done(&f.p, CURLE_ABORTED_BY_CALLBACK);
    assert(f.p.retry_queued && !f.p.probed);
    now += 0.5;
    assert(process_retries(&f.ctx) > 0 && starts == 0);
    now += 0.51;
    process_retries(&f.ctx);
    assert(starts == 1 && !f.p.read_timed_out);

    init(&f);
    f.net.timeout = 0;
    now = 100;
    assert(xferinfo_callback(&f.p, 0, 0, 0, 0) == 0);
    f.net.timeout = 1;
    f.p.paused = true;
    assert(xferinfo_callback(&f.p, 0, 0, 0, 0) == 0);
    now = 200;
    struct cmd resume = {.kind = CMD_UNPAUSE, .p = &f.p, .ctx = &f.ctx};
    run_cmd(&resume);
    assert(f.p.last_activity == 200 && !f.p.paused);
    now = 200.9;
    assert(xferinfo_callback(&f.p, 0, 0, 0, 0) == 0);
    now = 201.01;
    assert(xferinfo_callback(&f.p, 0, 0, 0, 0) == 1);

    init(&f);
    f.p.stream_ok = true;
    now = 0.9;
    assert(write_callback("data", 1, 4, &f.p) == 4);
    now = 1.1;
    assert(xferinfo_callback(&f.p, 0, 4, 0, 0) == 0);

    init(&f);
    f.p.probed = f.p.seekable = true;
    f.p.request_start = 100;
    f.p.request_received = 50;
    on_done(&f.p, CURLE_PARTIAL_FILE);
    assert(f.p.request_start == 150 && f.p.retry_at == 1);
    now = 1;
    process_retries(&f.ctx);
    on_done(&f.p, CURLE_RECV_ERROR);
    assert(f.p.retry_at == 4 && f.p.retry_count == 2);
    now = 4;
    process_retries(&f.ctx);
    on_done(&f.p, CURLE_RECV_ERROR);
    assert(f.p.stream_error && !f.ctx.retries && starts == 2);

    init(&f);
    f.p.probed = true;
    f.p.request_start = 100;
    f.p.request_received = 50;
    on_done(&f.p, CURLE_RECV_ERROR);
    assert(f.p.retry_queued && f.p.request_start == 0);
    struct cmd remove = {.kind = CMD_REMOVE, .p = &f.p, .ctx = &f.ctx};
    run_cmd(&remove);
    now = 10;
    process_retries(&f.ctx);
    assert(starts == 0 && !f.ctx.retries);
    on_done(&f.p, CURLE_RECV_ERROR);
    struct cmd seek = {.kind = CMD_SEEK, .p = &f.p, .ctx = &f.ctx,
                       .pos = 42, .drop = true};
    run_cmd(&seek);
    assert(!f.ctx.retries && f.p.request_start == 42 && starts == 1);

    init(&f);
    on_done(&f.p, CURLE_COULDNT_CONNECT);
    atomic_store(&f.p.aborted, true);
    process_retries(&f.ctx);
    assert(!f.ctx.retries && starts == 0);
    assert(xferinfo_callback(&f.p, 0, 0, 0, 0) == 1);

    init(&f);
    f.p.probed = true;
    f.opts.reconnect_streamed = false;
    on_done(&f.p, CURLE_RECV_ERROR);
    assert(f.p.stream_error && !f.ctx.retries);
    init(&f);
    f.scheme.proto = MP_CURL_PROTO_FTP;
    f.p.probed = true;
    on_done(&f.p, CURLE_RECV_ERROR);
    assert(f.p.stream_error && !f.ctx.retries);
    init(&f);
    on_done(&f.p, CURLE_PEER_FAILED_VERIFICATION);
    assert(f.p.probed && f.p.stream_error && !f.ctx.retries);
    init(&f);
    f.p.probed = true;
    on_done(&f.p, CURLE_OK);
    assert(f.p.stream_eof && !f.ctx.retries);

    init(&f);
    f.p.retry_count = 100;
    retry_request(&f.p);
    assert(f.p.retry_at == 5);
    cancel_retry(&f.p);
    f.opts.retry_delay_max = 0;
    retry_request(&f.p);
    assert(starts == 1 && !f.ctx.retries);
    puts("policy unit cases passed");
}

static void network_test(const char *url, const char *scenario)
{
    struct fixture f;
    init(&f);
    network = true;
    const char *mode = scenario;
    curl_global_init(CURL_GLOBAL_ALL);
    f.ctx.multi = curl_multi_init();
    f.p.curl = curl_easy_init();
    f.opts.max_retries = 1;
    f.opts.retry_delay_max = 1;
    curl_easy_setopt(f.p.curl, CURLOPT_URL, url);
    curl_easy_setopt(f.p.curl, CURLOPT_PROXY, "");
    curl_easy_setopt(f.p.curl, CURLOPT_CONNECTTIMEOUT_MS, 1000L);
    curl_easy_setopt(f.p.curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(f.p.curl, CURLOPT_WRITEDATA, &f.p);
    curl_easy_setopt(f.p.curl, CURLOPT_HEADERFUNCTION, header_callback);
    curl_easy_setopt(f.p.curl, CURLOPT_HEADERDATA, &f.p);
    curl_easy_setopt(f.p.curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(f.p.curl, CURLOPT_XFERINFOFUNCTION, xferinfo_callback);
    curl_easy_setopt(f.p.curl, CURLOPT_XFERINFODATA, &f.p);
    curl_easy_setopt(f.p.curl, CURLOPT_FOLLOWLOCATION, 1L);
    start_request(&f.p);
    double started = mp_time_sec();
    size_t body_bytes = 0;
    bool cancelled = false;
    while (!f.p.stream_eof && !f.p.stream_error) {
        double elapsed = mp_time_sec() - started;
        assert(elapsed < 12);
        if (!strcmp(mode, "cancel") && elapsed >= 0.2) {
            atomic_store(&f.p.aborted, true);
            cancelled = true;
        }
        process_retries(&f.ctx);
        int running;
        assert(curl_multi_perform(f.ctx.multi, &running) == CURLM_OK);
        CURLMsg *msg;
        int pending;
        while ((msg = curl_multi_info_read(f.ctx.multi, &pending))) {
            if (msg->msg != CURLMSG_DONE) continue;
            curl_multi_remove_handle(f.ctx.multi, f.p.curl);
            f.p.active = false;
            on_done(&f.p, msg->data.result);
        }
        if (strcmp(mode, "pause") || elapsed >= 3) {
            body_bytes += f.p.count;
            f.p.head = f.p.tail;
            f.p.count = 0;
            if (f.p.paused) {
                pauses++;
                struct cmd unpause = {.kind = CMD_UNPAUSE, .p = &f.p,
                                     .ctx = &f.ctx};
                run_cmd(&unpause);
            }
        }
        if (cancelled && !f.p.active && !f.ctx.retries) break;
        curl_multi_poll(f.ctx.multi, NULL, 0, 20, NULL);
    }
    body_bytes += f.p.count;
    printf("{\"attempts\":%d,\"bytes\":%zu,\"error\":%s,"
           "\"cancelled\":%s,\"pauses\":%d,\"elapsed\":%.3f}\n",
           starts, body_bytes, f.p.stream_error ? "true" : "false",
           cancelled ? "true" : "false", pauses, mp_time_sec() - started);
    cancel_retry(&f.p);
    if (f.p.active) curl_multi_remove_handle(f.ctx.multi, f.p.curl);
    curl_easy_cleanup(f.p.curl);
    curl_multi_cleanup(f.ctx.multi);
    curl_global_cleanup();
}
int main(int argc, char **argv)
{
    if (argc == 1) unit_tests();
    else { assert(argc == 3); network_test(argv[1], argv[2]); }
    return 0;
}
