#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

#define DEFAULT_NOFILE_LIMIT 131072ULL
#define AGENTGATEWAY_BINARY "/app/agentgateway"

static int parse_limit(const char *raw, rlim_t *result) {
    char *end = NULL;
    unsigned long long parsed;

    if (raw == NULL || *raw == '\0') {
        *result = (rlim_t)DEFAULT_NOFILE_LIMIT;
        return 0;
    }

    errno = 0;
    parsed = strtoull(raw, &end, 10);
    if (errno != 0 || end == raw || *end != '\0' || parsed == 0) {
        return -1;
    }

    *result = (rlim_t)parsed;
    if ((unsigned long long)*result != parsed) {
        return -1;
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *configured = getenv("AGENTGATEWAY_NOFILE_LIMIT");
    struct rlimit current;
    rlim_t requested;

    (void)argc;

    if (parse_limit(configured, &requested) != 0) {
        fprintf(stderr,
                "ERROR: AGENTGATEWAY_NOFILE_LIMIT must be a positive integer; got %s\n",
                configured == NULL ? "<unset>" : configured);
        return 64;
    }

    if (getrlimit(RLIMIT_NOFILE, &current) != 0) {
        fprintf(stderr, "ERROR: getrlimit(RLIMIT_NOFILE): %s\n", strerror(errno));
        return 78;
    }

    if (current.rlim_max != RLIM_INFINITY && requested > current.rlim_max) {
        fprintf(stderr,
                "ERROR: requested RLIMIT_NOFILE soft limit %llu exceeds container hard limit %llu\n",
                (unsigned long long)requested,
                (unsigned long long)current.rlim_max);
        return 78;
    }

    if (current.rlim_cur < requested) {
        current.rlim_cur = requested;
        if (setrlimit(RLIMIT_NOFILE, &current) != 0) {
            fprintf(stderr, "ERROR: setrlimit(RLIMIT_NOFILE): %s\n", strerror(errno));
            return 78;
        }
    }

    if (getrlimit(RLIMIT_NOFILE, &current) != 0 || current.rlim_cur < requested) {
        fprintf(stderr, "ERROR: RLIMIT_NOFILE verification failed\n");
        return 78;
    }

    if (current.rlim_max == RLIM_INFINITY) {
        fprintf(stderr,
                "agentgateway-entrypoint: RLIMIT_NOFILE soft=%llu hard=unlimited\n",
                (unsigned long long)current.rlim_cur);
    } else {
        fprintf(stderr,
                "agentgateway-entrypoint: RLIMIT_NOFILE soft=%llu hard=%llu\n",
                (unsigned long long)current.rlim_cur,
                (unsigned long long)current.rlim_max);
    }

    argv[0] = (char *)AGENTGATEWAY_BINARY;
    execv(AGENTGATEWAY_BINARY, argv);
    fprintf(stderr, "ERROR: execv(%s): %s\n", AGENTGATEWAY_BINARY, strerror(errno));
    return 126;
}
