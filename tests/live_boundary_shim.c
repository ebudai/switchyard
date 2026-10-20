/*
 * SYRD-627: the test boundary below the shell.
 *
 * `tests/live_boundary.py` builds this into each isolated run's disposable root
 * and preloads it (LD_PRELOAD) into every process of the run, namespace
 * children included. It refuses, before the call reaches the kernel:
 *   - a write, creation, removal, rename, link, permission, owner, time or
 *     extended-attribute change of anything under a live root (writes but no
 *     owner changes under a writable one), judged on the real target: after the
 *     shell has expanded it, relative to the real working directory or
 *     descriptor, through symlinks;
 *   - running the real sudo, pkexec, su, doas, runuser, systemctl, loginctl,
 *     machinectl, busctl or systemd-run (their queries aside), the real pkill or
 *     killall, or a kill of every process or of a process group not the run's;
 *   - a signal to every process or to a process group not the run's;
 *   - connecting to, or serving on, a socket in a shared /tmp/tmux-<uid>
 *     directory.
 * A refused call fails with EACCES (EPERM for a signal) and is reported on
 * stderr and in the run's log. The policy is read once, when the library loads, and every exec carries it on
 * unchanged: a child started with a cleared, emptied or replaced boundary variable is still behind the same
 * boundary. A child may only add to it -- another live root, a writable root that does not overlap a live root it
 * inherited, a later run start, another preload after this one.
 *
 * Not covered: statically linked programs, raw system calls, and setuid
 * programs (the loader drops the preload; the privileged ones are refused
 * before they run). The sentinel in live_boundary.py remains the backstop.
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/xattr.h>
#include <unistd.h>
#include <utime.h>

extern char **environ;

#define MAX_ROOTS 16

static char roots[MAX_ROOTS][PATH_MAX];
static int nroots = -1;
static char writable[MAX_ROOTS][PATH_MAX];
static int nwritable;
static char account_tmux[PATH_MAX];
static char shared_tmp[PATH_MAX];
static unsigned long long run_start;

/* The boundary's own variables, carried into every child. */
enum { PRELOAD, LIVE_ROOTS, WRITABLE, DISPOSABLE_ROOT, BOUNDARY_LOG, INVOKER_UID, RUN_START, NCARRIED };
static const char *carried[] = {"LD_PRELOAD", "SWITCHYARD_TEST_LIVE_ROOTS", "SWITCHYARD_TEST_WRITABLE",
                                "SWITCHYARD_TEST_DISPOSABLE_ROOT", "SWITCHYARD_TEST_BOUNDARY_LOG",
                                "SWITCHYARD_TEST_INVOKER_UID", "SWITCHYARD_TEST_RUN_START", NULL};
static char *carried_values[NCARRIED];

#define REAL(name) static __typeof__(name) *real_##name; \
    if (!real_##name) real_##name = (__typeof__(name) *)dlsym(RTLD_NEXT, #name)

static void split_roots(const char *value, char out[][PATH_MAX], int *count) {
    *count = 0;
    if (!value) return;
    const char *start = value;
    while (*start && *count < MAX_ROOTS) {
        const char *end = strchr(start, ':');
        size_t length = end ? (size_t)(end - start) : strlen(start);
        if (length && length < PATH_MAX) {
            char raw[PATH_MAX];
            memcpy(raw, start, length);
            raw[length] = 0;
            if (!realpath(raw, out[*count])) strcpy(out[*count], raw);
            (*count)++;
        }
        if (!end) break;
        start = end + 1;
    }
}

static void load(void) {
    if (nroots >= 0) return;
    int saved = errno;
    split_roots(getenv("SWITCHYARD_TEST_LIVE_ROOTS"), roots, &nroots);
    split_roots(getenv("SWITCHYARD_TEST_WRITABLE"), writable, &nwritable);
    const char *uid = getenv("SWITCHYARD_TEST_INVOKER_UID");
    snprintf(account_tmux, sizeof account_tmux, "/tmp/tmux-%s", uid ? uid : "");
    char resolved[PATH_MAX];
    if (realpath(account_tmux, resolved)) strcpy(account_tmux, resolved);
    if (!realpath("/tmp", shared_tmp)) strcpy(shared_tmp, "/tmp");
    const char *start = getenv("SWITCHYARD_TEST_RUN_START");
    run_start = start ? strtoull(start, NULL, 10) : 0;
    for (int i = 0; carried[i]; i++) {
        const char *value = getenv(carried[i]);
        carried_values[i] = value ? strdup(value) : NULL;
    }
    errno = saved;
}

/* Fixed when the library loads, before the program can change its own environment. */
__attribute__((constructor)) static void load_at_start(void) {
    load();
}

static int active(void) {
    load();
    return nroots > 0;
}

/* The run's start: the one this process was given, or a later one it has set since (never an earlier one). */
static unsigned long long run_boundary(void) {
    const char *current = getenv("SWITCHYARD_TEST_RUN_START");
    unsigned long long later = current ? strtoull(current, NULL, 10) : 0;
    return later > run_start ? later : run_start;
}

static int under(const char *path, const char *root) {
    size_t length = strlen(root);
    if (strncmp(path, root, length)) return 0;
    return path[length] == 0 || path[length] == '/' || (length && root[length - 1] == '/');
}

static void report(const char *what, const char *path) {
    char line[PATH_MAX + 256];
    char exe[PATH_MAX] = "?";
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n > 0) exe[n] = 0;
    int length = snprintf(line, sizeof line, "test boundary: refused %s %s (pid %d, uid %d, %s)\n", what,
                          path ? path : "", getpid(), getuid(), exe);
    if (length < 0) return;
    if ((size_t)length > sizeof line) length = sizeof line;
    ssize_t ignored = write(2, line, (size_t)length);
    (void)ignored;
    const char *log = getenv("SWITCHYARD_TEST_BOUNDARY_LOG");
    if (log && *log) {
        REAL(open);
        int fd = real_open(log, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
        if (fd >= 0) {
            ignored = write(fd, line, (size_t)length);
            close(fd);
        }
    }
}

/* An absolute, lexically normal form of `path` as seen from `dirfd`. */
static int absolute(int dirfd, const char *path, char *out) {
    char base[PATH_MAX];
    if (!path) path = "";
    if (path[0] == '/') {
        base[0] = 0;
    } else if (dirfd == AT_FDCWD) {
        if (!getcwd(base, sizeof base)) return -1;
    } else {
        char link[64];
        snprintf(link, sizeof link, "/proc/self/fd/%d", dirfd);
        ssize_t n = readlink(link, base, sizeof base - 1);
        if (n < 0) return -1;
        base[n] = 0;
    }
    char joined[PATH_MAX * 2];
    snprintf(joined, sizeof joined, "%s/%s", base, path);
    /* collapse //, /./ and /../ */
    char *parts[PATH_MAX / 2];
    int count = 0;
    char *save = NULL;
    for (char *part = strtok_r(joined, "/", &save); part; part = strtok_r(NULL, "/", &save)) {
        if (!strcmp(part, ".")) continue;
        if (!strcmp(part, "..")) {
            if (count) count--;
            continue;
        }
        parts[count++] = part;
    }
    size_t used = 0;
    out[0] = 0;
    for (int i = 0; i < count; i++) {
        size_t length = strlen(parts[i]);
        if (used + length + 2 >= PATH_MAX) return -1;
        out[used++] = '/';
        memcpy(out + used, parts[i], length);
        used += length;
    }
    if (!used) out[used++] = '/';
    out[used] = 0;
    return 0;
}

/* `path` (absolute, normal) with the longest prefix that exists resolved through its symlinks; when `follow` is
 * false the last component is kept as named and only its parent is resolved. */
static void resolved(const char *path, int follow, char *out) {
    if (!follow) {
        const char *slash = strrchr(path, '/');
        if (!slash || slash == path) {
            snprintf(out, PATH_MAX, "%s", path);
            return;
        }
        char parent[PATH_MAX], real[PATH_MAX];
        snprintf(parent, sizeof parent, "%.*s", (int)(slash - path), path);
        resolved(parent, 1, real);
        snprintf(out, PATH_MAX, "%s%s", strcmp(real, "/") ? real : "", slash);
        return;
    }
    char prefix[PATH_MAX], real[PATH_MAX];
    snprintf(prefix, sizeof prefix, "%s", path);
    size_t cut = strlen(prefix);
    for (;;) {
        if (realpath(prefix, real)) {
            snprintf(out, PATH_MAX, "%s%s", strcmp(real, "/") || !path[cut] ? real : "", path + cut);
            return;
        }
        char *slash = strrchr(prefix, '/');
        if (!slash || slash == prefix) break;
        *slash = 0;
        cut = (size_t)(slash - prefix);
    }
    snprintf(out, PATH_MAX, "%s", path);
}

static int blocked_one(const char *path, int chown) {
    int live = 0;
    for (int i = 0; i < nroots; i++) live |= under(path, roots[i]);
    if (!live) return 0;
    if (chown) return 1;
    for (int i = 0; i < nwritable; i++)
        if (under(path, writable[i])) return 0;
    return 1;
}

/* Whether a change to `path` (from `dirfd`) must be refused; reports it when so. */
static int refuse_path(const char *what, int dirfd, const char *path, int follow, int chown) {
    if (!active()) return 0;
    int saved = errno;
    char lexical[PATH_MAX], real[PATH_MAX];
    if (absolute(dirfd, path, lexical) < 0) {
        errno = saved;
        return 0;
    }
    resolved(lexical, follow, real);
    int refuse = blocked_one(lexical, chown) || blocked_one(real, chown);
    if (refuse) {
        report(what, real);
        errno = EACCES;
    } else {
        errno = saved;
    }
    return refuse;
}

static int refuse_fd(const char *what, int fd, int chown) {
    if (!active()) return 0;
    char link[64], path[PATH_MAX];
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof path - 1);
    if (n <= 0 || path[0] != '/') return 0;
    path[n] = 0;
    return refuse_path(what, AT_FDCWD, path, 1, chown);
}

static int writes(int flags) {
    return (flags & (O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND)) != 0 && !(flags & O_PATH);
}

static int mode_writes(const char *mode) {
    return mode && (strchr(mode, 'w') || strchr(mode, 'a') || strchr(mode, '+'));
}

/* ---- files ---------------------------------------------------------------------------------------------------- */

#define OPEN_MODE(flags, mode) do { \
        mode = 0; \
        if ((flags) & (O_CREAT | __O_TMPFILE)) { va_list args; va_start(args, flags); mode = va_arg(args, mode_t); va_end(args); } \
    } while (0)

int open(const char *path, int flags, ...) {
    REAL(open);
    mode_t mode;
    OPEN_MODE(flags, mode);
    if (writes(flags) && refuse_path("open for writing", AT_FDCWD, path, !(flags & O_NOFOLLOW), 0)) return -1;
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    REAL(open64);
    mode_t mode;
    OPEN_MODE(flags, mode);
    if (writes(flags) && refuse_path("open for writing", AT_FDCWD, path, !(flags & O_NOFOLLOW), 0)) return -1;
    return real_open64(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    REAL(openat);
    mode_t mode;
    OPEN_MODE(flags, mode);
    if (writes(flags) && refuse_path("open for writing", dirfd, path, !(flags & O_NOFOLLOW), 0)) return -1;
    return real_openat(dirfd, path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    REAL(openat64);
    mode_t mode;
    OPEN_MODE(flags, mode);
    if (writes(flags) && refuse_path("open for writing", dirfd, path, !(flags & O_NOFOLLOW), 0)) return -1;
    return real_openat64(dirfd, path, flags, mode);
}

int __open_2(const char *path, int flags) {
    REAL(__open_2);
    if (writes(flags) && refuse_path("open for writing", AT_FDCWD, path, 1, 0)) return -1;
    return real___open_2(path, flags);
}

int __open64_2(const char *path, int flags) {
    REAL(__open64_2);
    if (writes(flags) && refuse_path("open for writing", AT_FDCWD, path, 1, 0)) return -1;
    return real___open64_2(path, flags);
}

int __openat_2(int dirfd, const char *path, int flags) {
    REAL(__openat_2);
    if (writes(flags) && refuse_path("open for writing", dirfd, path, 1, 0)) return -1;
    return real___openat_2(dirfd, path, flags);
}

int __openat64_2(int dirfd, const char *path, int flags) {
    REAL(__openat64_2);
    if (writes(flags) && refuse_path("open for writing", dirfd, path, 1, 0)) return -1;
    return real___openat64_2(dirfd, path, flags);
}

int creat(const char *path, mode_t mode) {
    REAL(creat);
    if (refuse_path("create", AT_FDCWD, path, 1, 0)) return -1;
    return real_creat(path, mode);
}

int creat64(const char *path, mode_t mode) {
    REAL(creat64);
    if (refuse_path("create", AT_FDCWD, path, 1, 0)) return -1;
    return real_creat64(path, mode);
}

FILE *fopen(const char *path, const char *mode) {
    REAL(fopen);
    if (mode_writes(mode) && refuse_path("open for writing", AT_FDCWD, path, 1, 0)) return NULL;
    return real_fopen(path, mode);
}

FILE *fopen64(const char *path, const char *mode) {
    REAL(fopen64);
    if (mode_writes(mode) && refuse_path("open for writing", AT_FDCWD, path, 1, 0)) return NULL;
    return real_fopen64(path, mode);
}

FILE *freopen(const char *path, const char *mode, FILE *stream) {
    REAL(freopen);
    if (path && mode_writes(mode) && refuse_path("open for writing", AT_FDCWD, path, 1, 0)) return NULL;
    return real_freopen(path, mode, stream);
}

FILE *freopen64(const char *path, const char *mode, FILE *stream) {
    REAL(freopen64);
    if (path && mode_writes(mode) && refuse_path("open for writing", AT_FDCWD, path, 1, 0)) return NULL;
    return real_freopen64(path, mode, stream);
}

int mkdir(const char *path, mode_t mode) {
    REAL(mkdir);
    if (refuse_path("mkdir", AT_FDCWD, path, 0, 0)) return -1;
    return real_mkdir(path, mode);
}

int mkdirat(int dirfd, const char *path, mode_t mode) {
    REAL(mkdirat);
    if (refuse_path("mkdir", dirfd, path, 0, 0)) return -1;
    return real_mkdirat(dirfd, path, mode);
}

int rmdir(const char *path) {
    REAL(rmdir);
    if (refuse_path("rmdir", AT_FDCWD, path, 0, 0)) return -1;
    return real_rmdir(path);
}

int unlink(const char *path) {
    REAL(unlink);
    if (refuse_path("unlink", AT_FDCWD, path, 0, 0)) return -1;
    return real_unlink(path);
}

int unlinkat(int dirfd, const char *path, int flags) {
    REAL(unlinkat);
    if (refuse_path("unlink", dirfd, path, 0, 0)) return -1;
    return real_unlinkat(dirfd, path, flags);
}

int remove(const char *path) {
    REAL(remove);
    if (refuse_path("remove", AT_FDCWD, path, 0, 0)) return -1;
    return real_remove(path);
}

int rename(const char *from, const char *to) {
    REAL(rename);
    if (refuse_path("rename", AT_FDCWD, from, 0, 0) || refuse_path("rename", AT_FDCWD, to, 0, 0)) return -1;
    return real_rename(from, to);
}

int renameat(int fromfd, const char *from, int tofd, const char *to) {
    REAL(renameat);
    if (refuse_path("rename", fromfd, from, 0, 0) || refuse_path("rename", tofd, to, 0, 0)) return -1;
    return real_renameat(fromfd, from, tofd, to);
}

int renameat2(int fromfd, const char *from, int tofd, const char *to, unsigned int flags) {
    REAL(renameat2);
    if (refuse_path("rename", fromfd, from, 0, 0) || refuse_path("rename", tofd, to, 0, 0)) return -1;
    return real_renameat2(fromfd, from, tofd, to, flags);
}

int link(const char *from, const char *to) {
    REAL(link);
    if (refuse_path("link", AT_FDCWD, to, 0, 0)) return -1;
    return real_link(from, to);
}

int linkat(int fromfd, const char *from, int tofd, const char *to, int flags) {
    REAL(linkat);
    if (refuse_path("link", tofd, to, 0, 0)) return -1;
    return real_linkat(fromfd, from, tofd, to, flags);
}

int symlink(const char *target, const char *path) {
    REAL(symlink);
    if (refuse_path("symlink", AT_FDCWD, path, 0, 0)) return -1;
    return real_symlink(target, path);
}

int symlinkat(const char *target, int dirfd, const char *path) {
    REAL(symlinkat);
    if (refuse_path("symlink", dirfd, path, 0, 0)) return -1;
    return real_symlinkat(target, dirfd, path);
}

int chmod(const char *path, mode_t mode) {
    REAL(chmod);
    if (refuse_path("chmod", AT_FDCWD, path, 1, 0)) return -1;
    return real_chmod(path, mode);
}

int lchmod(const char *path, mode_t mode) {
    REAL(lchmod);
    if (refuse_path("chmod", AT_FDCWD, path, 0, 0)) return -1;
    return real_lchmod(path, mode);
}

int fchmodat(int dirfd, const char *path, mode_t mode, int flags) {
    REAL(fchmodat);
    if (refuse_path("chmod", dirfd, path, !(flags & AT_SYMLINK_NOFOLLOW), 0)) return -1;
    return real_fchmodat(dirfd, path, mode, flags);
}

int fchmod(int fd, mode_t mode) {
    REAL(fchmod);
    if (refuse_fd("chmod", fd, 0)) return -1;
    return real_fchmod(fd, mode);
}

int chown(const char *path, uid_t owner, gid_t group) {
    REAL(chown);
    if (refuse_path("chown", AT_FDCWD, path, 1, 1)) return -1;
    return real_chown(path, owner, group);
}

int lchown(const char *path, uid_t owner, gid_t group) {
    REAL(lchown);
    if (refuse_path("chown", AT_FDCWD, path, 0, 1)) return -1;
    return real_lchown(path, owner, group);
}

int fchownat(int dirfd, const char *path, uid_t owner, gid_t group, int flags) {
    REAL(fchownat);
    int follow = !(flags & AT_SYMLINK_NOFOLLOW);
    if (path && !*path && (flags & AT_EMPTY_PATH)) {
        if (refuse_fd("chown", dirfd, 1)) return -1;
    } else if (refuse_path("chown", dirfd, path, follow, 1)) {
        return -1;
    }
    return real_fchownat(dirfd, path, owner, group, flags);
}

int fchown(int fd, uid_t owner, gid_t group) {
    REAL(fchown);
    if (refuse_fd("chown", fd, 1)) return -1;
    return real_fchown(fd, owner, group);
}

int truncate(const char *path, off_t length) {
    REAL(truncate);
    if (refuse_path("truncate", AT_FDCWD, path, 1, 0)) return -1;
    return real_truncate(path, length);
}

int truncate64(const char *path, off64_t length) {
    REAL(truncate64);
    if (refuse_path("truncate", AT_FDCWD, path, 1, 0)) return -1;
    return real_truncate64(path, length);
}

int utimensat(int dirfd, const char *path, const struct timespec times[2], int flags) {
    REAL(utimensat);
    int refuse = path ? refuse_path("utime", dirfd, path, !(flags & AT_SYMLINK_NOFOLLOW), 0)
                      : refuse_fd("utime", dirfd, 0);
    if (refuse) return -1;
    return real_utimensat(dirfd, path, times, flags);
}

int futimens(int fd, const struct timespec times[2]) {
    REAL(futimens);
    if (refuse_fd("utime", fd, 0)) return -1;
    return real_futimens(fd, times);
}

int utimes(const char *path, const struct timeval times[2]) {
    REAL(utimes);
    if (refuse_path("utime", AT_FDCWD, path, 1, 0)) return -1;
    return real_utimes(path, times);
}

int utime(const char *path, const struct utimbuf *times) {
    REAL(utime);
    if (refuse_path("utime", AT_FDCWD, path, 1, 0)) return -1;
    return real_utime(path, times);
}

int mknod(const char *path, mode_t mode, dev_t dev) {
    REAL(mknod);
    if (refuse_path("mknod", AT_FDCWD, path, 0, 0)) return -1;
    return real_mknod(path, mode, dev);
}

int mknodat(int dirfd, const char *path, mode_t mode, dev_t dev) {
    REAL(mknodat);
    if (refuse_path("mknod", dirfd, path, 0, 0)) return -1;
    return real_mknodat(dirfd, path, mode, dev);
}

int mkfifo(const char *path, mode_t mode) {
    REAL(mkfifo);
    if (refuse_path("mkfifo", AT_FDCWD, path, 0, 0)) return -1;
    return real_mkfifo(path, mode);
}

int mkfifoat(int dirfd, const char *path, mode_t mode) {
    REAL(mkfifoat);
    if (refuse_path("mkfifo", dirfd, path, 0, 0)) return -1;
    return real_mkfifoat(dirfd, path, mode);
}

#define TEMPLATE(name, ...) \
    if (refuse_path("create", AT_FDCWD, template, 0, 0)) return -1; \
    return real_##name(__VA_ARGS__)

int mkstemp(char *template) { REAL(mkstemp); TEMPLATE(mkstemp, template); }
int mkostemp(char *template, int flags) { REAL(mkostemp); TEMPLATE(mkostemp, template, flags); }
int mkstemps(char *template, int suffix) { REAL(mkstemps); TEMPLATE(mkstemps, template, suffix); }
int mkostemps(char *template, int suffix, int flags) { REAL(mkostemps); TEMPLATE(mkostemps, template, suffix, flags); }
int mkstemp64(char *template) { REAL(mkstemp64); TEMPLATE(mkstemp64, template); }

char *mkdtemp(char *template) {
    REAL(mkdtemp);
    if (refuse_path("mkdir", AT_FDCWD, template, 0, 0)) return NULL;
    return real_mkdtemp(template);
}

#define XATTR(name, what, follow, ...) \
    if (refuse_path(what, AT_FDCWD, path, follow, 0)) return -1; \
    return real_##name(__VA_ARGS__)

int setxattr(const char *path, const char *name, const void *value, size_t size, int flags) {
    REAL(setxattr); XATTR(setxattr, "setxattr", 1, path, name, value, size, flags);
}
int lsetxattr(const char *path, const char *name, const void *value, size_t size, int flags) {
    REAL(lsetxattr); XATTR(lsetxattr, "setxattr", 0, path, name, value, size, flags);
}
int removexattr(const char *path, const char *name) { REAL(removexattr); XATTR(removexattr, "removexattr", 1, path, name); }
int lremovexattr(const char *path, const char *name) { REAL(lremovexattr); XATTR(lremovexattr, "removexattr", 0, path, name); }

int fsetxattr(int fd, const char *name, const void *value, size_t size, int flags) {
    REAL(fsetxattr);
    if (refuse_fd("setxattr", fd, 0)) return -1;
    return real_fsetxattr(fd, name, value, size, flags);
}

int fremovexattr(int fd, const char *name) {
    REAL(fremovexattr);
    if (refuse_fd("removexattr", fd, 0)) return -1;
    return real_fremovexattr(fd, name);
}

/* ---- sockets -------------------------------------------------------------------------------------------------- */

/* Whether a Unix socket path lies in a shared default tmux directory: the account's own, or any /tmp/tmux-<uid>. */
static int shared_tmux(const struct sockaddr *address, socklen_t length) {
    if (!active() || !address || address->sa_family != AF_UNIX || length <= (socklen_t)sizeof(sa_family_t)) return 0;
    const struct sockaddr_un *unix_address = (const struct sockaddr_un *)address;
    if (!unix_address->sun_path[0]) return 0;  /* abstract */
    char path[sizeof unix_address->sun_path + 1];
    size_t size = length - sizeof(sa_family_t);
    if (size > sizeof unix_address->sun_path) size = sizeof unix_address->sun_path;
    memcpy(path, unix_address->sun_path, size);
    path[size] = 0;
    char lexical[PATH_MAX], real[PATH_MAX];
    if (absolute(AT_FDCWD, path, lexical) < 0) return 0;
    resolved(lexical, 0, real);
    char *slash = strrchr(real, '/');
    if (!slash) return 0;
    *slash = 0;
    if (!strcmp(real, account_tmux)) return 1;
    size_t tmp = strlen(shared_tmp);
    if (strncmp(real, shared_tmp, tmp) || strncmp(real + tmp, "/tmux-", 6)) return 0;
    const char *digits = real + tmp + 6;
    if (!*digits) return 0;
    for (; *digits; digits++)
        if (*digits < '0' || *digits > '9') return 0;
    return 1;
}

int connect(int fd, const struct sockaddr *address, socklen_t length) {
    REAL(connect);
    if (shared_tmux(address, length)) {
        report("a connection to the shared tmux socket", ((const struct sockaddr_un *)address)->sun_path);
        errno = EACCES;
        return -1;
    }
    return real_connect(fd, address, length);
}

int bind(int fd, const struct sockaddr *address, socklen_t length) {
    REAL(bind);
    if (shared_tmux(address, length)) {
        report("a server on the shared tmux socket", ((const struct sockaddr_un *)address)->sun_path);
        errno = EACCES;
        return -1;
    }
    return real_bind(fd, address, length);
}

/* ---- signals -------------------------------------------------------------------------------------------------- */

static int stat_fields(pid_t pid, long long *parent, unsigned long long *start) {
    char path[64], text[1024];
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    REAL(open);
    int fd = real_open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t n = read(fd, text, sizeof text - 1);
    close(fd);
    if (n <= 0) return -1;
    text[n] = 0;
    char *after = strrchr(text, ')');
    if (!after) return -1;
    /* fields after the command: state(3) ppid(4) ... starttime(22) */
    char *save = NULL;
    int field = 3;
    for (char *token = strtok_r(after + 1, " ", &save); token; token = strtok_r(NULL, " ", &save), field++) {
        if (field == 4) *parent = atoll(token);
        if (field == 22) {
            *start = strtoull(token, NULL, 10);
            return 0;
        }
    }
    return -1;
}

/* Whether a process belongs to this run: started since it isolated itself, or a descendant of this process. */
static int ours(pid_t pid) {
    long long parent = 0;
    unsigned long long start = 0;
    if (stat_fields(pid, &parent, &start) < 0) return 1;  /* nothing there to harm */
    /* A process may mark a later start for its own run after the library loaded; an earlier one would widen it. */
    unsigned long long boundary = run_boundary();
    if (boundary && start >= boundary) return 1;
    pid_t me = getpid(), cursor = pid;
    for (int hops = 0; hops < 64; hops++) {
        if (cursor == me) return 1;
        if (stat_fields(cursor, &parent, &start) < 0 || parent <= 1) return 0;
        cursor = (pid_t)parent;
    }
    return 0;
}

static int broad_signal(pid_t pid, int sig) {
    if (!active() || sig == 0) return 0;
    return pid == -1 || (pid < -1 && !ours(-pid));
}

int kill(pid_t pid, int sig) {
    REAL(kill);
    if (broad_signal(pid, sig)) {
        report("a signal to every process or to a process group not this run's", "");
        errno = EPERM;
        return -1;
    }
    return real_kill(pid, sig);
}

int killpg(pid_t group, int sig) {
    REAL(killpg);
    if (active() && sig != 0 && group != 0 && (group == 1 || !ours(group))) {
        report("a signal to a process group not this run's", "");
        errno = EPERM;
        return -1;
    }
    return real_killpg(group, sig);
}

/* ---- programs ------------------------------------------------------------------------------------------------- */

static const char *privileged[] = {"sudo", "pkexec", "su", "doas", "runuser", "systemctl", "loginctl", "machinectl",
                                   "busctl", "systemd-run", NULL};
static const char *system_prefixes[] = {"/usr/", "/bin/", "/sbin/", "/lib", "/opt/", "/etc/", "/snap/", NULL};
static const char *value_options[] = {"-p", "--property", "-t", "--type", "-M", "--machine", "-H", "--host",
                                      "-n", "--lines", "-o", "--output", "--root", "--state", NULL};
static const char *queries[] = {
    /* systemctl */ "is-active", "is-enabled", "is-failed", "is-system-running", "status", "show", "cat", "list-units",
    "list-unit-files", "list-timers", "list-sockets", "list-dependencies", "get-default", "show-environment",
    /* loginctl */ "list-sessions", "list-users", "list-seats", "show-session", "show-user", "show-seat",
    "session-status", "user-status", "seat-status",
    /* machinectl, busctl */ "list", "list-images", "show-image", "image-status", "tree", "introspect",
    "get-property", NULL};

static int listed(const char *word, const char **list) {
    for (int i = 0; list[i]; i++)
        if (!strcmp(word, list[i])) return 1;
    return 0;
}

static const char *name_of(const char *path) {
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

/* The program `file` names, as exec would find it on `search` (a PATH). */
static int locate(const char *file, const char *search, char *out) {
    if (strchr(file, '/')) {
        if (!realpath(file, out)) snprintf(out, PATH_MAX, "%s", file);
        return 0;
    }
    if (!search) search = "/usr/local/bin:/usr/bin:/bin";
    const char *start = search;
    for (;;) {
        const char *end = strchr(start, ':');
        size_t length = end ? (size_t)(end - start) : strlen(start);
        char candidate[PATH_MAX];
        snprintf(candidate, sizeof candidate, "%.*s/%s", (int)length, length ? start : ".", file);
        if (access(candidate, X_OK) == 0) {
            if (!realpath(candidate, out)) snprintf(out, PATH_MAX, "%s", candidate);
            return 0;
        }
        if (!end) break;
        start = end + 1;
    }
    out[0] = 0;
    return -1;
}

static int real_system_tool(const char *resolved_path) {
    for (int i = 0; system_prefixes[i]; i++)
        if (!strncmp(resolved_path, system_prefixes[i], strlen(system_prefixes[i]))) return 1;
    return 0;
}

/* Whether running `program` (resolved) with `argv` is refused; reports it when so. */
static int refuse_program(const char *program, char *const argv[]) {
    if (!active() || !program || !*program) return 0;
    const char *name = name_of(program);
    if (!real_system_tool(program)) return 0;
    int saved = errno;
    char why[512] = "";
    if (listed(name, privileged)) {
        const char *verb = "";
        for (int i = 1; argv && argv[i]; i++) {
            if (listed(argv[i], value_options)) {
                if (argv[i + 1]) i++;
                continue;
            }
            if (argv[i][0] != '-') {
                verb = argv[i];
                break;
            }
        }
        int is_service = !strcmp(name, "systemctl") || !strcmp(name, "loginctl") || !strcmp(name, "machinectl")
                         || !strcmp(name, "busctl");
        if (!(is_service && listed(verb, queries))) snprintf(why, sizeof why, "the real %s", name);
    } else if (!strcmp(name, "pkill") || !strcmp(name, "killall")) {
        snprintf(why, sizeof why, "%s, which kills by name", name);
    } else if (!strcmp(name, "kill")) {
        int index = 1;
        if (argv && argv[index] && (!strcmp(argv[index], "-s") || !strcmp(argv[index], "-n"))) index += 2;
        else if (argv && argv[index] && argv[index][0] == '-' && strcmp(argv[index], "--")
                 && (argv[index][1] < '0' || argv[index][1] > '9')) index++;
        else if (argv && argv[index] && argv[index][0] == '-' && argv[index][1] >= '0' && argv[index][1] <= '9'
                 && argv[index + 1]) index++;
        for (; argv && argv[index]; index++) {
            if (!strcmp(argv[index], "--")) continue;
            const char *target = argv[index];
            if (target[0] == '-' && target[1] >= '0' && target[1] <= '9') {
                long group = atol(target + 1);
                if (group == 1 || !ours((pid_t)group))
                    snprintf(why, sizeof why, "a kill of every process or of a process group not this run's");
            }
        }
    }
    if (why[0]) {
        report(why, program);
        errno = EACCES;
        return 1;
    }
    errno = saved;
    return 0;
}

/* The value `envp` gives `name`, or NULL. */
static const char *given(char *const envp[], const char *name) {
    size_t length = strlen(name);
    for (int i = 0; envp && envp[i]; i++)
        if (!strncmp(envp[i], name, length) && envp[i][length] == '=') return envp[i] + length + 1;
    return NULL;
}

/* Whether the ':'-separated `list` holds `item` (`length` bytes). */
static int holds(const char *list, const char *item, size_t length) {
    for (const char *start = list; start;) {
        const char *end = strchr(start, ':');
        size_t size = end ? (size_t)(end - start) : strlen(start);
        if (size == length && !strncmp(start, item, length)) return 1;
        start = end ? end + 1 : NULL;
    }
    return 0;
}

/* Whether a writable root a child names would open up any live root this process carries. */
static int overlaps_live(const char *item, size_t length) {
    char raw[PATH_MAX], real[PATH_MAX], lexical[PATH_MAX];
    if (!length || length >= PATH_MAX || item[0] != '/') return 1;
    memcpy(raw, item, length);
    raw[length] = 0;
    if (absolute(AT_FDCWD, raw, lexical) < 0) return 1;
    resolved(lexical, 1, real);
    for (int i = 0; i < nroots; i++)
        if (under(lexical, roots[i]) || under(roots[i], lexical) || under(real, roots[i]) || under(roots[i], real))
            return 1;
    return 0;
}

/* `name=` this process's own value, then each item of the child's that it lacks (and, for writable roots, that
 * opens up no live root). */
static char *joined(int k, const char *child) {
    const char *own = carried_values[k];
    size_t size = strlen(carried[k]) + strlen(own) + (child ? strlen(child) : 0) + 3;
    char *entry = malloc(size);
    if (!entry) return NULL;
    int used = snprintf(entry, size, "%s=%s", carried[k], own);
    for (const char *start = child; start && used >= 0;) {
        const char *end = strchr(start, ':');
        size_t length = end ? (size_t)(end - start) : strlen(start);
        if (length && !holds(own, start, length) && !(k == WRITABLE && overlaps_live(start, length)))
            used += snprintf(entry + used, size - (size_t)used, "%s%.*s", used > (int)strlen(carried[k]) + 1 ? ":" : "",
                             (int)length, start);
        start = end ? end + 1 : NULL;
    }
    return entry;
}

/* `envp` behind this process's boundary: whatever it says about the boundary's own variables, the child gets
 * this process's values, plus only what narrows nothing (see the top of this file). NULL when out of memory. */
static char **carry(char *const envp[]) {
    load();
    int count = 0;
    while (envp && envp[count]) count++;
    char **out = malloc(sizeof(char *) * (size_t)(count + NCARRIED + 1));
    if (!out) return NULL;
    int used = 0;
    for (int i = 0; i < count; i++) {
        int reserved = 0;
        for (int k = 0; carried[k]; k++)
            if (carried_values[k] && !strncmp(envp[i], carried[k], strlen(carried[k]))
                && envp[i][strlen(carried[k])] == '=') reserved = 1;
        if (!reserved) out[used++] = envp[i];
    }
    for (int k = 0; carried[k]; k++) {
        if (!carried_values[k]) continue;  /* not part of this process's boundary: the child's own, if any, stays */
        const char *child = given(envp, carried[k]);
        char *entry;
        if (k == PRELOAD || k == LIVE_ROOTS || k == WRITABLE) {
            entry = joined(k, child);
        } else if (k == RUN_START) {
            /* The later of this process's start and the child's: a later one narrows the run, an earlier one not. */
            unsigned long long own = run_boundary(), theirs = child ? strtoull(child, NULL, 10) : 0;
            entry = malloc(strlen(carried[k]) + 24);
            if (entry) sprintf(entry, "%s=%llu", carried[k], theirs > own ? theirs : own);
        } else {
            entry = malloc(strlen(carried[k]) + strlen(carried_values[k]) + 2);
            if (entry) sprintf(entry, "%s=%s", carried[k], carried_values[k]);
        }
        if (!entry) {
            free(out);
            return NULL;
        }
        out[used++] = entry;
    }
    out[used] = NULL;
    return out;
}

int execve(const char *path, char *const argv[], char *const envp[]) {
    REAL(execve);
    if (active()) {
        char program[PATH_MAX], named[PATH_MAX];
        snprintf(named, sizeof named, "%s%s", strchr(path, '/') ? "" : "./", path);  /* execve does not search */
        locate(named, NULL, program);
        if (refuse_program(program, argv)) return -1;
        char **carried_env = carry(envp);
        if (!carried_env) {
            errno = ENOMEM;
            return -1;
        }
        return real_execve(path, argv, carried_env);
    }
    return real_execve(path, argv, envp);
}

int execv(const char *path, char *const argv[]) {
    return execve(path, argv, environ);
}

static int search_exec(const char *file, char *const argv[], char *const envp[]) {
    char program[PATH_MAX];
    if (locate(file, getenv("PATH"), program) < 0) {
        errno = ENOENT;
        return -1;
    }
    if (refuse_program(program, argv)) return -1;
    return execve(program, argv, envp);
}

int execvp(const char *file, char *const argv[]) {
    if (!active()) {
        REAL(execvp);
        return real_execvp(file, argv);
    }
    return search_exec(file, argv, environ);
}

int execvpe(const char *file, char *const argv[], char *const envp[]) {
    if (!active()) {
        REAL(execvpe);
        return real_execvpe(file, argv, envp);
    }
    return search_exec(file, argv, envp);
}

static char **collect(const char *first, va_list args, char ***envp) {
    size_t count = 1;
    va_list copy;
    va_copy(copy, args);
    while (va_arg(copy, char *)) count++;
    va_end(copy);
    char **argv = malloc(sizeof(char *) * (count + 1));
    if (!argv) return NULL;
    argv[0] = (char *)first;
    for (size_t i = 1; i <= count; i++) argv[i] = va_arg(args, char *);
    if (envp) *envp = va_arg(args, char **);
    return argv;
}

int execl(const char *path, const char *arg, ...) {
    va_list args;
    va_start(args, arg);
    char **argv = collect(arg, args, NULL);
    va_end(args);
    if (!argv) return -1;
    return execve(path, argv, environ);
}

int execlp(const char *file, const char *arg, ...) {
    va_list args;
    va_start(args, arg);
    char **argv = collect(arg, args, NULL);
    va_end(args);
    if (!argv) return -1;
    return execvp(file, argv);
}

int execle(const char *path, const char *arg, ...) {
    va_list args;
    va_start(args, arg);
    char **envp = NULL;
    char **argv = collect(arg, args, &envp);
    va_end(args);
    if (!argv) return -1;
    return execve(path, argv, envp);
}

int posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *actions,
                const posix_spawnattr_t *attributes, char *const argv[], char *const envp[]) {
    REAL(posix_spawn);
    if (active()) {
        char program[PATH_MAX], named[PATH_MAX];
        snprintf(named, sizeof named, "%s%s", strchr(path, '/') ? "" : "./", path);
        locate(named, NULL, program);
        if (refuse_program(program, argv)) return EACCES;
        char **carried_env = carry(envp);
        if (!carried_env) return ENOMEM;
        int spawned = real_posix_spawn(pid, path, actions, attributes, argv, carried_env);
        free(carried_env);
        return spawned;
    }
    return real_posix_spawn(pid, path, actions, attributes, argv, envp);
}

int posix_spawnp(pid_t *pid, const char *file, const posix_spawn_file_actions_t *actions,
                 const posix_spawnattr_t *attributes, char *const argv[], char *const envp[]) {
    REAL(posix_spawnp);
    if (active()) {
        char program[PATH_MAX];
        locate(file, getenv("PATH"), program);  /* posix_spawnp searches the caller's PATH */
        if (refuse_program(program, argv)) return EACCES;
        char **carried_env = carry(envp);
        if (!carried_env) return ENOMEM;
        int spawned = real_posix_spawnp(pid, file, actions, attributes, argv, carried_env);
        free(carried_env);
        return spawned;
    }
    return real_posix_spawnp(pid, file, actions, attributes, argv, envp);
}
