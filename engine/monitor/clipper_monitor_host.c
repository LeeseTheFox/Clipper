/*
 * clipper_monitor_host.c - user-scoped host process monitor for Clipper.
 *
 * This helper is intended to run through flatpak-spawn --host. It reads only
 * Clipper's config and the current user's visible /proc data, then sends
 * newline-delimited JSON process events to the sandboxed Clipper app.
 */

#define _GNU_SOURCE

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"

#define MAX_RULES 256
#define MAX_PROCESSES 4096
#define FIELD_MAX 1024
#define CMDLINE_MAX_BYTES 8192
#define RULE_ID_MAX 256
#define MAX_NAMESPACE_PIDS 8
#define DEFAULT_INTERVAL_MS 5000
#define DELETED_PATH_SUFFIX " (deleted)"
#define OBS_STUDIO_RULE_ID "clipper-observer-obs-studio"

typedef struct {
    char id[RULE_ID_MAX];
    char name[FIELD_MAX];
    char path[FIELD_MAX];
    char executable_path[FIELD_MAX];
    char executable_name[FIELD_MAX];
    char match_mode[64];
    char flatpak_id[FIELD_MAX];
    char process_name[FIELD_MAX];
    char install_path[FIELD_MAX];
    char steam_installation[FIELD_MAX];
    char appid[64];
    char capture_mode[64];
} monitor_rule_t;

typedef struct {
    int pid;
    int namespace_pids[MAX_NAMESPACE_PIDS];
    size_t namespace_pid_count;
    char comm[FIELD_MAX];
    char cmdline[CMDLINE_MAX_BYTES];
    char environ[CMDLINE_MAX_BYTES];
    char flatpak_id[FIELD_MAX];
    char exe[PATH_MAX];
    char cwd[PATH_MAX];
} process_info_t;

typedef struct {
    monitor_rule_t rules[MAX_RULES];
    size_t rule_count;
} monitor_config_t;

typedef struct {
    bool exists;
    off_t size;
    time_t mtime;
    long mtime_nsec;
} config_stamp_t;

typedef struct {
    bool active;
    int pid;
    char name[FIELD_MAX];
} rule_state_t;

typedef enum {
    STEAM_VARIANT_NATIVE,
    STEAM_VARIANT_FLATPAK,
    STEAM_VARIANT_SNAP,
} steam_variant_t;

static const char *string_or_empty(const cJSON *item)
{
    const char *value = cJSON_GetStringValue(item);
    return value ? value : "";
}

static void copy_string(char *dest, size_t size, const char *src)
{
    size_t len;
    if (!dest || size == 0)
        return;
    if (!src)
        src = "";
    len = strlen(src);
    if (len >= size)
        len = size - 1;
    memcpy(dest, src, len);
    dest[len] = '\0';
}

static unsigned long long stable_rule_hash(const char *value)
{
    const unsigned char *ptr = (const unsigned char *)(value ? value : "");
    unsigned long long hash = 14695981039346656037ULL;
    while (*ptr) {
        hash ^= (unsigned long long)*ptr++;
        hash *= 1099511628211ULL;
    }
    return hash;
}

static void lowercase_copy(char *dest, size_t size, const char *src)
{
    size_t idx = 0;
    if (!dest || size == 0)
        return;
    for (; src && src[idx] && idx + 1 < size; idx++)
        dest[idx] = (char)tolower((unsigned char)src[idx]);
    dest[idx] = '\0';
}

static const char *basename_ptr(const char *path)
{
    const char *slash;
    if (!path || !*path)
        return "";
    slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

static void strip_deleted_suffix(char *value)
{
    size_t len;
    size_t suffix_len = strlen(DELETED_PATH_SUFFIX);
    if (!value)
        return;
    len = strlen(value);
    if (len >= suffix_len &&
        strcmp(value + len - suffix_len, DELETED_PATH_SUFFIX) == 0) {
        value[len - suffix_len] = '\0';
    }
}

static void basename_copy(char *dest, size_t size, const char *path)
{
    char copy[PATH_MAX];
    copy_string(copy, sizeof(copy), path);
    strip_deleted_suffix(copy);
    copy_string(dest, size, basename_ptr(copy));
}

static bool string_contains_casefold(const char *haystack, const char *needle)
{
    char h[CMDLINE_MAX_BYTES];
    char n[FIELD_MAX];
    if (!haystack || !needle || !*haystack || !*needle)
        return false;
    lowercase_copy(h, sizeof(h), haystack);
    lowercase_copy(n, sizeof(n), needle);
    return strstr(h, n) != NULL;
}

static bool string_equals_casefold(const char *first, const char *second)
{
    char a[FIELD_MAX];
    char b[FIELD_MAX];
    lowercase_copy(a, sizeof(a), first);
    lowercase_copy(b, sizeof(b), second);
    return strcmp(a, b) == 0;
}

static bool is_path_boundary(char ch)
{
    return ch == '\0' || ch == '/' || isspace((unsigned char)ch) || ch == '"' ||
           ch == '\'' || ch == ';' || ch == ':';
}

static bool same_or_descendant_path_casefold(const char *path, const char *root)
{
    char p[CMDLINE_MAX_BYTES];
    char r[FIELD_MAX];
    size_t len;
    if (!path || !root || !*path || !*root)
        return false;
    lowercase_copy(p, sizeof(p), path);
    lowercase_copy(r, sizeof(r), root);
    strip_deleted_suffix(p);
    strip_deleted_suffix(r);
    len = strlen(r);
    while (len > 1 && r[len - 1] == '/')
        r[--len] = '\0';
    if (strcmp(p, r) == 0)
        return true;
    return strncmp(p, r, len) == 0 && p[len] == '/';
}

static size_t append_path_variant(char variants[][CMDLINE_MAX_BYTES],
                                  size_t count,
                                  size_t max_count,
                                  const char *value)
{
    char normalized[CMDLINE_MAX_BYTES];
    size_t len;
    if (!value || !*value || count >= max_count)
        return count;

    copy_string(normalized, sizeof(normalized), value);
    strip_deleted_suffix(normalized);
    len = strlen(normalized);
    while (len > 1 && normalized[len - 1] == '/')
        normalized[--len] = '\0';
    if (!normalized[0])
        return count;

    for (size_t idx = 0; idx < count; idx++) {
        if (strcmp(variants[idx], normalized) == 0)
            return count;
    }

    copy_string(variants[count], CMDLINE_MAX_BYTES, normalized);
    return count + 1;
}

static size_t append_home_alias_variant(char variants[][CMDLINE_MAX_BYTES],
                                        size_t count,
                                        size_t max_count,
                                        const char *value)
{
    char aliased[CMDLINE_MAX_BYTES];
    if (!value || !*value)
        return count;

    if (strncmp(value, "/home/", 6) == 0) {
        snprintf(aliased, sizeof(aliased), "/var/home/%s", value + 6);
        return append_path_variant(variants, count, max_count, aliased);
    }
    if (strncmp(value, "/var/home/", 10) == 0) {
        snprintf(aliased, sizeof(aliased), "/home/%s", value + 10);
        return append_path_variant(variants, count, max_count, aliased);
    }
    return count;
}

static size_t append_realpath_variant(char variants[][CMDLINE_MAX_BYTES],
                                      size_t count,
                                      size_t max_count,
                                      const char *value)
{
    char path[CMDLINE_MAX_BYTES];
    char resolved[PATH_MAX];
    if (!value || !*value)
        return count;

    copy_string(path, sizeof(path), value);
    strip_deleted_suffix(path);
    if (!realpath(path, resolved))
        return count;

    count = append_path_variant(variants, count, max_count, resolved);
    return append_home_alias_variant(variants, count, max_count, resolved);
}

static size_t path_variants(char variants[][CMDLINE_MAX_BYTES],
                            size_t max_count,
                            const char *value)
{
    size_t count = append_path_variant(variants, 0, max_count, value);
    count = append_home_alias_variant(variants, count, max_count, value);
    return append_realpath_variant(variants, count, max_count, value);
}

static bool same_or_descendant_path_with_aliases(const char *path, const char *root)
{
    char path_options[4][CMDLINE_MAX_BYTES];
    char root_options[4][CMDLINE_MAX_BYTES];
    size_t path_count = path_variants(path_options, 4, path);
    size_t root_count = path_variants(root_options, 4, root);

    for (size_t path_idx = 0; path_idx < path_count; path_idx++) {
        for (size_t root_idx = 0; root_idx < root_count; root_idx++) {
            if (same_or_descendant_path_casefold(path_options[path_idx],
                                                 root_options[root_idx])) {
                return true;
            }
        }
    }

    return false;
}

static bool text_mentions_path_casefold(const char *text, const char *path)
{
    char h[CMDLINE_MAX_BYTES];
    char n[FIELD_MAX];
    size_t len;
    size_t offset = 0;
    if (!text || !path || !*text || !*path)
        return false;
    lowercase_copy(h, sizeof(h), text);
    lowercase_copy(n, sizeof(n), path);
    strip_deleted_suffix(n);
    len = strlen(n);
    while (len > 1 && n[len - 1] == '/')
        n[--len] = '\0';
    while (true) {
        char *match = strstr(h + offset, n);
        if (!match)
            return false;
        if (is_path_boundary(match[len]))
            return true;
        offset = (size_t)(match - h) + 1;
    }
}

static bool text_mentions_path_with_aliases(const char *text, const char *path)
{
    char options[4][CMDLINE_MAX_BYTES];
    size_t count = path_variants(options, 4, path);

    for (size_t idx = 0; idx < count; idx++) {
        if (text_mentions_path_casefold(text, options[idx]))
            return true;
    }
    return false;
}

static bool is_name_boundary(char ch)
{
    return ch == '\0' ||
           !(isalnum((unsigned char)ch) || ch == '_' || ch == '.' || ch == '-');
}

static bool text_mentions_name_casefold(const char *text, const char *name)
{
    char h[CMDLINE_MAX_BYTES];
    char n[FIELD_MAX];
    size_t len;
    size_t offset = 0;
    if (!text || !name || !*text || !*name)
        return false;
    lowercase_copy(h, sizeof(h), text);
    lowercase_copy(n, sizeof(n), name);
    len = strlen(n);
    while (true) {
        char *match = strstr(h + offset, n);
        char before;
        if (!match)
            return false;
        before = match == h ? '\0' : match[-1];
        if (is_name_boundary(before) && is_name_boundary(match[len]))
            return true;
        offset = (size_t)(match - h) + 1;
    }
}

static bool path_or_text_mentions(const process_info_t *proc, const char *value)
{
    if (!value || !*value)
        return false;
    return same_or_descendant_path_with_aliases(proc->exe, value) ||
           same_or_descendant_path_with_aliases(proc->cwd, value) ||
           text_mentions_path_with_aliases(proc->cmdline, value) ||
           text_mentions_path_with_aliases(proc->environ, value);
}

static bool name_matches_process(const process_info_t *proc, const char *name)
{
    char exe_name[FIELD_MAX];
    if (!name || !*name)
        return false;
    basename_copy(exe_name, sizeof(exe_name), proc->exe);
    return string_equals_casefold(proc->comm, name) ||
           string_equals_casefold(exe_name, name) ||
           text_mentions_name_casefold(proc->cmdline, name) ||
           text_mentions_name_casefold(proc->environ, name);
}

static bool contains_steam_appid(const char *value, const char *appid)
{
    char copy[CMDLINE_MAX_BYTES];
    char *saveptr = NULL;
    char *token;
    char compat[128];
    if (!value || !appid || !*value || !*appid)
        return false;

    copy_string(copy, sizeof(copy), value);
    token = strtok_r(copy, " \t\r\n;", &saveptr);
    while (token) {
        char *equals = strchr(token, '=');
        if (equals) {
            *equals = '\0';
            if (strcmp(equals + 1, appid) == 0 &&
                (string_equals_casefold(token, "steamappid") ||
                 string_equals_casefold(token, "steamgameid") ||
                 string_equals_casefold(token, "steam_app_id") ||
                 string_equals_casefold(token, "steam_game_id") ||
                 string_equals_casefold(token, "steam_compat_app_id") ||
                 string_equals_casefold(token, "steam_compat_data_app_id") ||
                 string_equals_casefold(token, "steamoverlaygameid"))) {
                return true;
            }
        }
        token = strtok_r(NULL, " \t\r\n;", &saveptr);
    }

    snprintf(compat, sizeof(compat), "compatdata/%s", appid);
    if (string_contains_casefold(value, compat)) {
        size_t offset = 0;
        char lower[CMDLINE_MAX_BYTES];
        lowercase_copy(lower, sizeof(lower), value);
        while (true) {
            char *match = strstr(lower + offset, compat);
            char next;
            if (!match)
                break;
            next = match[strlen(compat)];
            if (next == '\0' || next == '/' || isspace((unsigned char)next))
                return true;
            offset = (size_t)(match - lower) + 1;
        }
    }
    return false;
}

static void process_flatpak_id(char *dest, size_t size, const process_info_t *proc)
{
    if (proc->flatpak_id[0]) {
        copy_string(dest, size, proc->flatpak_id);
        return;
    }
    char copy[CMDLINE_MAX_BYTES];
    char *saveptr = NULL;
    copy_string(copy, sizeof(copy), proc->environ);
    dest[0] = '\0';
    for (char *token = strtok_r(copy, " \t\r\n", &saveptr); token;
         token = strtok_r(NULL, " \t\r\n", &saveptr)) {
        if (strncmp(token, "FLATPAK_ID=", 11) == 0) {
            copy_string(dest, size, token + 11);
            return;
        }
    }
}

static bool exact_executable_path(const char *actual, const char *expected)
{
    char path[PATH_MAX];
    copy_string(path, sizeof(path), actual);
    strip_deleted_suffix(path);
    if (!path[0] || !expected[0])
        return false;
    if (strcmp(path, expected) == 0)
        return true;
    const char *a = strncmp(path, "/var/home/", 10) == 0 ? path + 4 : path;
    const char *b = strncmp(expected, "/var/home/", 10) == 0 ? expected + 4 : expected;
    return strcmp(a, b) == 0;
}

static bool rule_matches_process(const monitor_rule_t *rule, const process_info_t *proc)
{
    if (rule->flatpak_id[0]) {
        char app_id[FIELD_MAX];
        process_flatpak_id(app_id, sizeof(app_id), proc);
        if (strcmp(app_id, rule->flatpak_id) != 0)
            return false;
    }
    if (!rule->appid[0] && strcmp(rule->match_mode, "executable") == 0) {
        return exact_executable_path(proc->exe,
                                    rule->executable_path[0] ? rule->executable_path : rule->path) &&
               (!rule->process_name[0] || strcmp(rule->process_name, proc->comm) == 0);
    }
    if (rule->steam_installation[0]) {
        bool flatpak = text_mentions_name_casefold(proc->environ,
                                                  "FLATPAK_ID=com.valvesoftware.Steam");
        bool snap = text_mentions_name_casefold(proc->environ, "SNAP_NAME=steam");
        bool wants_flatpak = strncmp(rule->steam_installation, "flatpak_steam_", 14) == 0;
        bool wants_snap = strncmp(rule->steam_installation, "steam_snap:", 11) == 0;
        if (flatpak != wants_flatpak || snap != wants_snap)
            return false;
    }
    if (path_or_text_mentions(proc, rule->install_path))
        return true;

    if (contains_steam_appid(proc->cmdline, rule->appid) ||
        contains_steam_appid(proc->environ, rule->appid) ||
        contains_steam_appid(proc->exe, rule->appid) ||
        contains_steam_appid(proc->cwd, rule->appid)) {
        return true;
    }

    if (path_or_text_mentions(proc, rule->executable_path))
        return true;
    if (path_or_text_mentions(proc, rule->path))
        return true;

    if (name_matches_process(proc, rule->executable_name))
        return true;

    if (rule->path[0] && strchr(rule->path, '/') == NULL &&
        name_matches_process(proc, rule->path)) {
        return true;
    }
    if (name_matches_process(proc, rule->name))
        return true;

    return false;
}

static void default_config_path(char *buf, size_t size)
{
    const char *override = getenv("CLIPPER_CONFIG_FILE");
    const char *xdg = getenv("XDG_CONFIG_HOME");
    const char *home = getenv("HOME");
    if (override && *override) {
        copy_string(buf, size, override);
    } else if (xdg && *xdg) {
        snprintf(buf, size, "%s/clipper/config.json", xdg);
    } else if (home && *home) {
        snprintf(buf, size, "%s/.config/clipper/config.json", home);
    } else {
        copy_string(buf, size, "config.json");
    }
}

static void default_socket_path(char *buf, size_t size)
{
    const char *override = getenv("CLIPPER_MONITOR_SOCKET");
    const char *xdg = getenv("XDG_RUNTIME_DIR");
    if (override && *override) {
        copy_string(buf, size, override);
    } else if (xdg && *xdg) {
        snprintf(buf, size, "%s/clipper/monitor.sock", xdg);
    } else {
        snprintf(buf, size, "/tmp/clipper-monitor-%u.sock", (unsigned)getuid());
    }
}

static const char *proc_root(void)
{
    const char *override = getenv("CLIPPER_PROC_ROOT");
    return (override && *override) ? override : "/proc";
}

static char *read_file_alloc(const char *path, size_t limit)
{
    FILE *fp = fopen(path, "rb");
    char *buf;
    size_t used = 0;
    int ch;
    if (!fp)
        return NULL;
    buf = calloc(limit + 1, 1);
    if (!buf) {
        fclose(fp);
        return NULL;
    }
    while (used < limit && (ch = fgetc(fp)) != EOF)
        buf[used++] = (char)(ch == '\0' ? ' ' : ch);
    buf[used] = '\0';
    fclose(fp);
    return buf;
}

static void read_text_field(char *dest, size_t size, const char *path)
{
    char *text = read_file_alloc(path, size - 1);
    if (!text) {
        dest[0] = '\0';
        return;
    }
    text[strcspn(text, "\r\n")] = '\0';
    copy_string(dest, size, text);
    free(text);
}

static void read_link_field(char *dest, size_t size, const char *path)
{
    ssize_t len = readlink(path, dest, size - 1);
    if (len < 0) {
        dest[0] = '\0';
        return;
    }
    dest[len] = '\0';
}

static void read_namespace_pids(process_info_t *proc, const char *path)
{
    FILE *fp = fopen(path, "r");
    char line[FIELD_MAX];

    if (!fp)
        return;

    while (fgets(line, sizeof(line), fp)) {
        char *cursor;

        if (strncmp(line, "NSpid:", 6) != 0)
            continue;

        cursor = line + 6;
        while (*cursor && proc->namespace_pid_count < MAX_NAMESPACE_PIDS) {
            char *end = NULL;
            long pid;

            while (isspace((unsigned char)*cursor))
                cursor++;
            if (!*cursor)
                break;

            pid = strtol(cursor, &end, 10);
            if (end == cursor)
                break;
            if (pid > 0 && pid <= INT_MAX)
                proc->namespace_pids[proc->namespace_pid_count++] = (int)pid;
            cursor = end;
        }
        break;
    }

    fclose(fp);
}

static bool load_config(monitor_config_t *config)
{
    char path[PATH_MAX];
    char *text;
    cJSON *root;
    cJSON *whitelist;
    int count;
    config->rule_count = 0;
    default_config_path(path, sizeof(path));
    text = read_file_alloc(path, 1024 * 1024);
    if (!text) {
        fprintf(stderr, "[clipper-monitor] no config at %s\n", path);
        return true;
    }

    root = cJSON_Parse(text);
    free(text);
    if (!root) {
        fprintf(stderr, "[clipper-monitor] invalid config JSON\n");
        return false;
    }

    whitelist = cJSON_GetObjectItemCaseSensitive(root, "whitelist");
    if (!cJSON_IsArray(whitelist)) {
        cJSON_Delete(root);
        return true;
    }

    count = cJSON_GetArraySize(whitelist);
    for (int idx = 0; idx < count && config->rule_count < MAX_RULES; idx++) {
        cJSON *entry = cJSON_GetArrayItem(whitelist, idx);
        monitor_rule_t *rule;
        if (!cJSON_IsObject(entry))
            continue;
        rule = &config->rules[config->rule_count];
        memset(rule, 0, sizeof(*rule));
        copy_string(rule->name, sizeof(rule->name),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "name")));
        copy_string(rule->path, sizeof(rule->path),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "path")));
        copy_string(rule->executable_path, sizeof(rule->executable_path),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "executable_path")));
        copy_string(rule->executable_name, sizeof(rule->executable_name),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "executable_name")));
        copy_string(rule->match_mode, sizeof(rule->match_mode),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "match_mode")));
        copy_string(rule->flatpak_id, sizeof(rule->flatpak_id),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "flatpak_id")));
        copy_string(rule->process_name, sizeof(rule->process_name),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "process_name")));
        copy_string(rule->install_path, sizeof(rule->install_path),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "install_path")));
        copy_string(rule->appid, sizeof(rule->appid),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "appid")));
        copy_string(rule->steam_installation, sizeof(rule->steam_installation),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "steam_installation")));
        copy_string(rule->capture_mode, sizeof(rule->capture_mode),
                    string_or_empty(cJSON_GetObjectItemCaseSensitive(entry, "capture_mode")));
        if (!rule->capture_mode[0])
            copy_string(rule->capture_mode, sizeof(rule->capture_mode), "display_capture");
        if (rule->appid[0]) {
            char appid[sizeof(rule->appid)];
            copy_string(appid, sizeof(appid), rule->appid);
            if (rule->steam_installation[0])
                snprintf(rule->id, sizeof(rule->id), "steam-%016llx-%s",
                         stable_rule_hash(rule->steam_installation), appid);
            else
                snprintf(rule->id, sizeof(rule->id), "steam-%s", appid);
        } else if (strcmp(rule->match_mode, "executable") == 0) {
            char identity[FIELD_MAX * 3 + 3];
            snprintf(identity, sizeof(identity), "%s\n%s\n%s", rule->flatpak_id,
                     rule->executable_path, rule->process_name);
            snprintf(rule->id, sizeof(rule->id), "executable-%016llx",
                     stable_rule_hash(identity));
        } else if (rule->executable_path[0]) {
            snprintf(rule->id, sizeof(rule->id), "path-%016llx",
                     stable_rule_hash(rule->executable_path));
        } else {
            snprintf(rule->id, sizeof(rule->id), "rule-%zu", config->rule_count);
        }
        config->rule_count++;
    }

    cJSON_Delete(root);
    return true;
}

static config_stamp_t current_config_stamp(void)
{
    char path[PATH_MAX];
    struct stat st;
    config_stamp_t stamp = {0};
    default_config_path(path, sizeof(path));
    if (stat(path, &st) == 0) {
        stamp.exists = true;
        stamp.size = st.st_size;
        stamp.mtime = st.st_mtime;
        stamp.mtime_nsec = st.st_mtim.tv_nsec;
    }
    return stamp;
}

static bool config_stamp_equal(config_stamp_t first, config_stamp_t second)
{
    return first.exists == second.exists && first.size == second.size &&
           first.mtime == second.mtime && first.mtime_nsec == second.mtime_nsec;
}

static void preserve_states_for_reloaded_config(rule_state_t *states,
                                                const monitor_config_t *old_config,
                                                const monitor_config_t *new_config)
{
    rule_state_t old_states[MAX_RULES];
    memcpy(old_states, states, sizeof(old_states));
    memset(states, 0, sizeof(old_states));

    for (size_t new_idx = 0; new_idx < new_config->rule_count; new_idx++) {
        for (size_t old_idx = 0; old_idx < old_config->rule_count; old_idx++) {
            if (strcmp(new_config->rules[new_idx].id, old_config->rules[old_idx].id) == 0) {
                states[new_idx] = old_states[old_idx];
                break;
            }
        }
    }
}

static void read_flatpak_identity(process_info_t *proc, const char *path)
{
    FILE *file = fopen(path, "r");
    char line[FIELD_MAX];
    bool application = false;
    if (!file)
        return;
    while (fgets(line, sizeof(line), file)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '[')
            application = strcmp(line, "[Application]") == 0;
        else if (application && strncmp(line, "name=", 5) == 0) {
            copy_string(proc->flatpak_id, sizeof(proc->flatpak_id), line + 5);
            break;
        }
    }
    fclose(file);
}

static bool read_processes(process_info_t *processes, size_t max_processes,
                           size_t *count_out)
{
    const char *root = proc_root();
    DIR *dir = opendir(root);
    struct dirent *entry;
    size_t count = 0;
    if (!dir) {
        *count_out = 0;
        return false;
    }

    while ((entry = readdir(dir)) != NULL && count < max_processes) {
        char path[PATH_MAX];
        char *end = NULL;
        long pid;
        process_info_t *proc;
        if (!isdigit((unsigned char)entry->d_name[0]))
            continue;
        pid = strtol(entry->d_name, &end, 10);
        if (!end || *end != '\0' || pid <= 0)
            continue;

        proc = &processes[count];
        memset(proc, 0, sizeof(*proc));
        proc->pid = (int)pid;

        snprintf(path, sizeof(path), "%s/%s/status", root, entry->d_name);
        read_namespace_pids(proc, path);
        snprintf(path, sizeof(path), "%s/%s/comm", root, entry->d_name);
        read_text_field(proc->comm, sizeof(proc->comm), path);
        snprintf(path, sizeof(path), "%s/%s/cmdline", root, entry->d_name);
        read_text_field(proc->cmdline, sizeof(proc->cmdline), path);
        snprintf(path, sizeof(path), "%s/%s/environ", root, entry->d_name);
        read_text_field(proc->environ, sizeof(proc->environ), path);
        snprintf(path, sizeof(path), "%s/%s/root/.flatpak-info", root, entry->d_name);
        read_flatpak_identity(proc, path);
        snprintf(path, sizeof(path), "%s/%s/exe", root, entry->d_name);
        read_link_field(proc->exe, sizeof(proc->exe), path);
        snprintf(path, sizeof(path), "%s/%s/cwd", root, entry->d_name);
        read_link_field(proc->cwd, sizeof(proc->cwd), path);

        if (proc->comm[0] || proc->cmdline[0] || proc->environ[0] ||
            proc->exe[0] || proc->cwd[0]) {
            count++;
        }
    }

    closedir(dir);
    *count_out = count;
    return true;
}

static bool steam_process_matches_variant(const process_info_t *proc,
                                          steam_variant_t variant)
{
    bool flatpak = text_mentions_name_casefold(
        proc->environ, "FLATPAK_ID=com.valvesoftware.Steam");
    bool snap = text_mentions_name_casefold(proc->environ, "SNAP_NAME=steam") ||
                text_mentions_name_casefold(proc->environ,
                                            "SNAP_INSTANCE_NAME=steam");

    if (variant == STEAM_VARIANT_FLATPAK)
        return flatpak;
    if (variant == STEAM_VARIANT_SNAP)
        return snap;
    return !flatpak && !snap;
}

static bool steam_is_running(steam_variant_t variant)
{
    DIR *directory = opendir(proc_root());
    struct dirent *entry;
    if (!directory)
        return false;
    while ((entry = readdir(directory))) {
        char path[PATH_MAX];
        process_info_t process = {0};
        if (!isdigit((unsigned char)entry->d_name[0]))
            continue;
        snprintf(path, sizeof(path), "%s/%s/comm", proc_root(), entry->d_name);
        read_text_field(process.comm, sizeof(process.comm), path);
        if (strcmp(process.comm, "steam") != 0 && strcmp(process.comm, "steamwebhelper") != 0)
            continue;
        snprintf(path, sizeof(path), "%s/%s/environ", proc_root(), entry->d_name);
        read_text_field(process.environ, sizeof(process.environ), path);
        if (steam_process_matches_variant(&process, variant)) {
            closedir(directory);
            return true;
        }
    }
    closedir(directory);
    return false;
}

static void redirect_stdio_to_devnull(void)
{
    FILE *devnull = fopen("/dev/null", "r+");
    if (!devnull)
        return;
    dup2(fileno(devnull), STDIN_FILENO);
    dup2(fileno(devnull), STDOUT_FILENO);
    dup2(fileno(devnull), STDERR_FILENO);
    if (fileno(devnull) > STDERR_FILENO)
        fclose(devnull);
}

static void restore_host_session_bus(void)
{
    const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
    char address[PATH_MAX];
    if (!runtime_dir || !*runtime_dir)
        return;
    snprintf(address, sizeof(address), "unix:path=%s/bus", runtime_dir);
    setenv("DBUS_SESSION_BUS_ADDRESS", address, 1);
    /* Flatpak's app-specific XDG directories must not become Steam's home. */
    unsetenv("XDG_DATA_HOME");
    unsetenv("XDG_CONFIG_HOME");
    unsetenv("XDG_CACHE_HOME");
    unsetenv("FLATPAK_ID");
}

static void exec_steam_direct(steam_variant_t variant, bool shutdown)
{
    const char *argument = shutdown ? "-shutdown" : "steam://open/main";
    if (variant == STEAM_VARIANT_FLATPAK)
        execlp("flatpak", "flatpak", "run",
               "com.valvesoftware.Steam", argument, (char *)NULL);
    else if (variant == STEAM_VARIANT_SNAP)
        execlp("snap", "snap", "run", "steam", argument, (char *)NULL);
    else
        execlp("steam", "steam", argument, (char *)NULL);
}

static void exec_steam_systemd(steam_variant_t variant)
{
    if (variant == STEAM_VARIANT_FLATPAK)
        execlp("systemd-run", "systemd-run", "--user", "--collect", "--quiet",
               "--", "flatpak", "run", "com.valvesoftware.Steam",
               "steam://open/main", (char *)NULL);
    else if (variant == STEAM_VARIANT_SNAP)
        execlp("systemd-run", "systemd-run", "--user", "--collect", "--quiet",
               "--", "snap", "run", "steam", "steam://open/main", (char *)NULL);
    else
        execlp("systemd-run", "systemd-run", "--user", "--collect", "--quiet",
               "--", "steam", "steam://open/main", (char *)NULL);
}

static int run_steam_command(steam_variant_t variant, bool shutdown)
{
    pid_t pid;
    int status = 0;

    /* A fixed stop request for an absent variant must not invoke another
     * Steam package's shared desktop/IPC handling by accident. */
    if (shutdown && !steam_is_running(variant))
        return 0;

    pid = fork();

    if (pid < 0) {
        fprintf(stderr, "[clipper-monitor] could not fork Steam command: %s\n",
                strerror(errno));
        return 2;
    }
    if (pid == 0) {
        redirect_stdio_to_devnull();
        restore_host_session_bus();
        if (shutdown)
            exec_steam_direct(variant, true);
        else
            exec_steam_systemd(variant);
        _exit(127);
    }

    if (shutdown) {
        if (waitpid(pid, &status, 0) < 0)
            return 2;
        for (int attempt = 0; attempt < 300; attempt++) {
            if (!steam_is_running(variant))
                return 0;
            usleep(100000);
        }
        fprintf(stderr, "[clipper-monitor] Steam did not stop within 30 seconds\n");
        return 3;
    }

    for (int attempt = 0; attempt < 300; attempt++) {
        pid_t finished = waitpid(pid, &status, WNOHANG);
        if (finished == pid && WIFEXITED(status) && WEXITSTATUS(status) == 127) {
            fprintf(stderr, "[clipper-monitor] could not start Steam\n");
            return 2;
        }
        if (steam_is_running(variant)) {
            return 0;
        }
        usleep(100000);
    }
    fprintf(stderr, "[clipper-monitor] Steam did not start within 30 seconds\n");
    return 3;
}

static bool find_match_for_rule(const monitor_rule_t *rule,
                                const process_info_t *processes, size_t count,
                                process_info_t *matched)
{
    for (size_t idx = 0; idx < count; idx++) {
        if (rule_matches_process(rule, &processes[idx])) {
            if (matched)
                *matched = processes[idx];
            return true;
        }
    }
    return false;
}

static bool process_is_obs_studio(const process_info_t *proc)
{
    char exe_name[FIELD_MAX];
    basename_copy(exe_name, sizeof(exe_name), proc->exe);

    /* Exact executable names avoid matching clipper-engine merely because it
     * links libobs, or games whose environment mentions obs-gamecapture. */
    return string_equals_casefold(proc->comm, "obs") ||
           string_equals_casefold(proc->comm, "obs-studio") ||
           string_equals_casefold(exe_name, "obs") ||
           string_equals_casefold(exe_name, "obs-studio");
}

static bool find_obs_studio_process(const process_info_t *processes, size_t count,
                                    process_info_t *matched)
{
    for (size_t idx = 0; idx < count; idx++) {
        if (process_is_obs_studio(&processes[idx])) {
            if (matched)
                *matched = processes[idx];
            return true;
        }
    }
    return false;
}

static int connect_monitor_socket(void)
{
    char socket_path[PATH_MAX];
    struct sockaddr_un addr;
    int fd;
    default_socket_path(socket_path, sizeof(socket_path));

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(socket_path) >= sizeof(addr.sun_path)) {
        close(fd);
        errno = ENAMETOOLONG;
        return -1;
    }
    copy_string(addr.sun_path, sizeof(addr.sun_path), socket_path);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static const char *process_name(const process_info_t *proc)
{
    if (proc->comm[0])
        return proc->comm;
    if (proc->exe[0])
        return basename_ptr(proc->exe);
    return "process";
}

static int list_processes(void)
{
    process_info_t *processes = calloc(MAX_PROCESSES, sizeof(process_info_t));
    monitor_config_t config;
    cJSON *array;
    char *text;
    size_t count;

    if (!processes)
        return 2;
    if (!load_config(&config)) {
        free(processes);
        return 2;
    }

    if (!read_processes(processes, MAX_PROCESSES, &count)) {
        fprintf(stderr, "[clipper-monitor] could not read process snapshot: %s\n",
                strerror(errno));
        free(processes);
        return 2;
    }
    array = cJSON_CreateArray();
    if (!array) {
        free(processes);
        return 2;
    }

    for (size_t idx = 0; idx < count; idx++) {
        cJSON *obj = cJSON_CreateObject();
        if (!obj || !cJSON_AddItemToArray(array, obj)) {
            cJSON_Delete(obj);
            cJSON_Delete(array);
            free(processes);
            return 2;
        }
        cJSON_AddNumberToObject(obj, "pid", processes[idx].pid);
        cJSON_AddStringToObject(obj, "comm", processes[idx].comm);
        cJSON_AddStringToObject(obj, "cmdline", processes[idx].cmdline);
        cJSON_AddStringToObject(obj, "exe", processes[idx].exe);
        char app_id[FIELD_MAX];
        process_flatpak_id(app_id, sizeof(app_id), &processes[idx]);
        if (app_id[0])
            cJSON_AddStringToObject(obj, "flatpak_id", app_id);
        cJSON *namespace_pids = cJSON_AddArrayToObject(obj, "namespace_pids");
        if (!namespace_pids) {
            cJSON_Delete(array);
            free(processes);
            return 2;
        }
        for (size_t pid_idx = 0;
             pid_idx < processes[idx].namespace_pid_count;
             pid_idx++) {
            cJSON_AddItemToArray(
                namespace_pids,
                cJSON_CreateNumber(processes[idx].namespace_pids[pid_idx]));
        }
        cJSON *rule_ids = cJSON_AddArrayToObject(obj, "rule_ids");
        if (!rule_ids) {
            cJSON_Delete(array);
            free(processes);
            return 2;
        }
        for (size_t rule_idx = 0; rule_idx < config.rule_count; rule_idx++) {
            if (rule_matches_process(&config.rules[rule_idx], &processes[idx]))
                cJSON_AddItemToArray(
                    rule_ids, cJSON_CreateString(config.rules[rule_idx].id));
        }
    }

    text = cJSON_PrintUnformatted(array);
    cJSON_Delete(array);
    free(processes);
    if (!text)
        return 2;

    printf("%s\n", text);
    free(text);
    return 0;
}

static bool send_event(int fd, const char *event, const monitor_rule_t *rule,
                       const process_info_t *proc)
{
    cJSON *obj = cJSON_CreateObject();
    char *text;
    bool ok = false;
    if (!obj)
        return false;
    cJSON_AddStringToObject(obj, "event", event);
    cJSON_AddStringToObject(obj, "rule_id", rule->id);
    cJSON_AddStringToObject(obj, "name", rule->name[0] ? rule->name : process_name(proc));
    if (proc)
        cJSON_AddNumberToObject(obj, "pid", proc->pid);
    text = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (!text)
        return false;
    if (dprintf(fd, "%s\n", text) >= 0)
        ok = true;
    free(text);
    return ok;
}

static int run_once(bool require_socket)
{
    monitor_config_t config;
    process_info_t *processes;
    size_t process_count;
    int fd = -1;
    int matches = 0;
    if (!load_config(&config))
        return 2;
    processes = calloc(MAX_PROCESSES, sizeof(process_info_t));
    if (!processes)
        return 2;
    if (!read_processes(processes, MAX_PROCESSES, &process_count)) {
        fprintf(stderr, "[clipper-monitor] could not read process snapshot: %s\n",
                strerror(errno));
        free(processes);
        return 2;
    }
    if (require_socket) {
        fd = connect_monitor_socket();
        if (fd < 0) {
            fprintf(stderr, "[clipper-monitor] could not connect to monitor socket: %s\n",
                    strerror(errno));
            free(processes);
            return 3;
        }
    }
    for (size_t idx = 0; idx < config.rule_count; idx++) {
        process_info_t proc;
        if (find_match_for_rule(&config.rules[idx], processes, process_count, &proc)) {
            matches++;
            if (fd >= 0)
                send_event(fd, "process_started", &config.rules[idx], &proc);
        }
    }
    if (fd >= 0)
        close(fd);
    free(processes);
    printf("{\"ok\":true,\"rules\":%zu,\"matches\":%d}\n", config.rule_count, matches);
    return 0;
}

static int interval_ms(void)
{
    const char *value = getenv("CLIPPER_MONITOR_INTERVAL_MS");
    int ms = value ? atoi(value) : 0;
    return ms > 0 ? ms : DEFAULT_INTERVAL_MS;
}

static int max_iterations(void)
{
    const char *value = getenv("CLIPPER_MONITOR_MAX_ITERATIONS");
    int iterations = value ? atoi(value) : 0;
    return iterations > 0 ? iterations : 0;
}

static int run_service(void)
{
    monitor_config_t config;
    rule_state_t states[MAX_RULES] = {0};
    rule_state_t obs_state = {0};
    const monitor_rule_t obs_rule = {
        .id = OBS_STUDIO_RULE_ID,
        .name = "OBS Studio",
    };
    int fd = -1;
    int sleep_ms = interval_ms();
    int max_loops = max_iterations();
    int loops = 0;
    config_stamp_t stamp;
    if (!load_config(&config))
        return 2;
    stamp = current_config_stamp();

    for (;;) {
        process_info_t *processes;
        size_t process_count = 0;
        config_stamp_t current_stamp = current_config_stamp();
        if (!config_stamp_equal(stamp, current_stamp)) {
            monitor_config_t reloaded;
            if (load_config(&reloaded)) {
                monitor_config_t old_config = config;
                preserve_states_for_reloaded_config(states, &old_config, &reloaded);
                config = reloaded;
                stamp = current_stamp;
                fprintf(stderr, "[clipper-monitor] reloaded config rules=%zu\n",
                        config.rule_count);
            }
        }

        if (fd < 0)
            fd = connect_monitor_socket();

        processes = calloc(MAX_PROCESSES, sizeof(process_info_t));
        if (!processes) {
            fprintf(stderr, "[clipper-monitor] could not allocate process snapshot: %s\n",
                    strerror(errno));
        } else if (!read_processes(processes, MAX_PROCESSES, &process_count)) {
            fprintf(stderr, "[clipper-monitor] could not read process snapshot: %s\n",
                    strerror(errno));
        } else {
            process_info_t proc;
            bool matched = find_obs_studio_process(processes, process_count, &proc);
            if (matched && !obs_state.active) {
                obs_state.active = true;
                obs_state.pid = proc.pid;
                copy_string(obs_state.name, sizeof(obs_state.name), process_name(&proc));
                fprintf(stderr, "[clipper-monitor] started %s pid=%d\n",
                        obs_rule.id, proc.pid);
                if (fd >= 0 && !send_event(fd, "process_started", &obs_rule, &proc)) {
                    close(fd);
                    fd = -1;
                }
            } else if (!matched && obs_state.active) {
                process_info_t stopped = {0};
                stopped.pid = obs_state.pid;
                copy_string(stopped.comm, sizeof(stopped.comm), obs_state.name);
                obs_state.active = false;
                fprintf(stderr, "[clipper-monitor] stopped %s\n", obs_rule.id);
                if (fd >= 0 &&
                    !send_event(fd, "process_stopped", &obs_rule, &stopped)) {
                    close(fd);
                    fd = -1;
                }
            }

            for (size_t idx = 0; idx < config.rule_count; idx++) {
                matched = find_match_for_rule(&config.rules[idx], processes,
                                              process_count, &proc);
                if (matched && !states[idx].active) {
                    states[idx].active = true;
                    states[idx].pid = proc.pid;
                    copy_string(states[idx].name, sizeof(states[idx].name),
                                process_name(&proc));
                    fprintf(stderr, "[clipper-monitor] started %s pid=%d\n",
                            config.rules[idx].id, proc.pid);
                    if (fd >= 0 &&
                        !send_event(fd, "process_started", &config.rules[idx], &proc)) {
                        close(fd);
                        fd = -1;
                    }
                } else if (!matched && states[idx].active) {
                    process_info_t stopped = {0};
                    stopped.pid = states[idx].pid;
                    copy_string(stopped.comm, sizeof(stopped.comm), states[idx].name);
                    states[idx].active = false;
                    fprintf(stderr, "[clipper-monitor] stopped %s\n",
                            config.rules[idx].id);
                    if (fd >= 0 &&
                        !send_event(fd, "process_stopped", &config.rules[idx], &stopped)) {
                        close(fd);
                        fd = -1;
                    }
                }
            }
        }
        free(processes);
        loops++;
        if (max_loops > 0 && loops >= max_loops)
            break;
        usleep((useconds_t)sleep_ms * 1000);
    }
    if (fd >= 0)
        close(fd);
    return 0;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
            "Usage: %s --probe|--once|--service|--list-processes|"
            "--stop-steam-{native,flatpak,snap}|"
            "--start-steam-{native,flatpak,snap}|"
            "--steam-running-{native,flatpak,snap}\n",
            argv0);
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        usage(argv[0]);
        return 64;
    }
    if (strcmp(argv[1], "--probe") == 0)
        return run_once(false);
    if (strcmp(argv[1], "--once") == 0)
        return run_once(true);
    if (strcmp(argv[1], "--service") == 0)
        return run_service();
    if (strcmp(argv[1], "--list-processes") == 0)
        return list_processes();
    if (strcmp(argv[1], "--stop-steam-native") == 0)
        return run_steam_command(STEAM_VARIANT_NATIVE, true);
    if (strcmp(argv[1], "--start-steam-native") == 0)
        return run_steam_command(STEAM_VARIANT_NATIVE, false);
    if (strcmp(argv[1], "--stop-steam-flatpak") == 0)
        return run_steam_command(STEAM_VARIANT_FLATPAK, true);
    if (strcmp(argv[1], "--start-steam-flatpak") == 0)
        return run_steam_command(STEAM_VARIANT_FLATPAK, false);
    if (strcmp(argv[1], "--stop-steam-snap") == 0)
        return run_steam_command(STEAM_VARIANT_SNAP, true);
    if (strcmp(argv[1], "--start-steam-snap") == 0)
        return run_steam_command(STEAM_VARIANT_SNAP, false);
    if (strcmp(argv[1], "--steam-running-native") == 0)
        return steam_is_running(STEAM_VARIANT_NATIVE) ? 0 : 1;
    if (strcmp(argv[1], "--steam-running-flatpak") == 0)
        return steam_is_running(STEAM_VARIANT_FLATPAK) ? 0 : 1;
    if (strcmp(argv[1], "--steam-running-snap") == 0)
        return steam_is_running(STEAM_VARIANT_SNAP) ? 0 : 1;
    usage(argv[0]);
    return 64;
}
