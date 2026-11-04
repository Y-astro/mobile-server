#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dlfcn.h>
#include <netdb.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <stdint.h>
#include <stddef.h>
#include <pthread.h>

#define MAGIC 0x444E534649583032ULL // "DNSFIX02"

typedef int (*orig_getaddrinfo_fn)(const char *node, const char *service,
                                   const struct addrinfo *hints,
                                   struct addrinfo **res);
typedef void (*orig_freeaddrinfo_fn)(struct addrinfo *res);

static orig_getaddrinfo_fn orig_getaddrinfo = NULL;
static orig_freeaddrinfo_fn orig_freeaddrinfo = NULL;
static pthread_once_t init_once = PTHREAD_ONCE_INIT;

static void do_init_orig(void) {
    orig_getaddrinfo = (orig_getaddrinfo_fn)dlsym(RTLD_NEXT, "getaddrinfo");
    orig_freeaddrinfo = (orig_freeaddrinfo_fn)dlsym(RTLD_NEXT, "freeaddrinfo");
}

static void init_orig(void) {
    pthread_once(&init_once, do_init_orig);
}

struct my_ai {
    uint64_t magic;
    union {
        struct sockaddr_in sin;
        struct sockaddr_in6 sin6;
    } addr;
    struct addrinfo ai;
};

static const struct {
    const char *ip;
    int port;
} UPSTREAM_DNS[] = {
    {"100.100.100.100", 53},   // Tailscale MagicDNS
    {"127.0.0.1", 5353},       // Local AdGuard Home
    {"192.168.1.1", 53},       // Local Wi-Fi router
    {"8.8.8.8", 53},           // Google DNS
    {"1.1.1.1", 53}            // Cloudflare DNS
};
#define NUM_UPSTREAM (sizeof(UPSTREAM_DNS) / sizeof(UPSTREAM_DNS[0]))

static int query_dns_udp(const char *server, int port, const char *name, uint32_t *ips, int max_ips) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) return 0;

    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 400000; // 400ms timeout per upstream
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    struct sockaddr_in saddr;
    memset(&saddr, 0, sizeof(saddr));
    saddr.sin_family = AF_INET;
    saddr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, server, &saddr.sin_addr) <= 0) {
        close(sock);
        return 0;
    }

    uint8_t pkt[512];
    memset(pkt, 0, sizeof(pkt));
    pkt[0] = 0xab; pkt[1] = 0xcd; // ID
    pkt[2] = 0x01; pkt[3] = 0x00; // Standard query, RD=1
    pkt[5] = 0x01;                 // QDCOUNT = 1

    int idx = 12;
    const char *p = name;
    while (*p) {
        const char *dot = strchr(p, '.');
        int len = dot ? (int)(dot - p) : (int)strlen(p);
        if (len <= 0 || len > 63 || idx + 1 + len >= 400) { close(sock); return 0; }
        pkt[idx++] = (uint8_t)len;
        memcpy(&pkt[idx], p, len);
        idx += len;
        if (!dot) break;
        p = dot + 1;
    }
    pkt[idx++] = 0;    // Terminating null
    pkt[idx++] = 0x00; // QTYPE = 1 (A)
    pkt[idx++] = 0x01;
    pkt[idx++] = 0x00; // QCLASS = 1 (IN)
    pkt[idx++] = 0x01;

    if (sendto(sock, pkt, idx, 0, (struct sockaddr *)&saddr, sizeof(saddr)) != idx) {
        close(sock);
        return 0;
    }

    uint8_t resp[1024];
    socklen_t slen = sizeof(saddr);
    ssize_t n = recvfrom(sock, resp, sizeof(resp), 0, (struct sockaddr *)&saddr, &slen);
    close(sock);

    if (n < 12) return 0;
    if (resp[0] != 0xab || resp[1] != 0xcd) return 0;
    if ((resp[3] & 0x0f) != 0) return 0; // Not NOERROR

    int ancount = (resp[6] << 8) | resp[7];
    if (ancount <= 0) return 0;

    int pos = 12;
    while (pos < n && resp[pos] != 0) {
        pos += 1 + resp[pos];
    }
    pos += 5; // null + qtype(2) + qclass(2)
    if (pos >= n) return 0;

    int found = 0;
    for (int a = 0; a < ancount && pos < n && found < max_ips; a++) {
        if ((resp[pos] & 0xc0) == 0xc0) {
            pos += 2;
        } else {
            while (pos < n && resp[pos] != 0) {
                pos += 1 + resp[pos];
            }
            pos++;
        }
        if (pos + 10 > n) break;
        uint16_t type = (resp[pos] << 8) | resp[pos + 1];
        uint16_t rdlen = (resp[pos + 8] << 8) | resp[pos + 9];
        pos += 10;
        if (pos + rdlen > n) break;

        if (type == 1 && rdlen == 4) { // IPv4 A record
            uint32_t ip;
            memcpy(&ip, &resp[pos], 4);
            ips[found++] = ip;
        }
        pos += rdlen;
    }
    return found;
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints,
                struct addrinfo **res) {
    init_orig();
    if (!res) return EAI_NONAME;

    // Pass through if node is null
    if (!node) {
        return orig_getaddrinfo(node, service, hints, res);
    }

    // Pass through numeric IPv4 or IPv6 literals
    struct in_addr in4;
    struct in6_addr in6;
    if (inet_pton(AF_INET, node, &in4) == 1 || inet_pton(AF_INET6, node, &in6) == 1) {
        return orig_getaddrinfo(node, service, hints, res);
    }

    // Localhost handling
    if (strcasecmp(node, "localhost") == 0) {
        return orig_getaddrinfo(node, service, hints, res);
    }

    // If caller explicitly and exclusively requested AF_INET6, pass through
    if (hints && hints->ai_family == AF_INET6) {
        return orig_getaddrinfo(node, service, hints, res);
    }

    // Query upstream DNS servers in order
    uint32_t ips[8];
    int count = 0;
    for (size_t i = 0; i < NUM_UPSTREAM; i++) {
        count = query_dns_udp(UPSTREAM_DNS[i].ip, UPSTREAM_DNS[i].port, node, ips, 8);
        if (count > 0) break;
    }

    if (count <= 0) {
        // Fallback to system getaddrinfo if direct UDP failed
        return orig_getaddrinfo(node, service, hints, res);
    }

    int port = 0;
    if (service) {
        if (strcmp(service, "http") == 0) port = 80;
        else if (strcmp(service, "https") == 0) port = 443;
        else port = atoi(service);
    }

    int socktype = (hints && hints->ai_socktype) ? hints->ai_socktype : SOCK_STREAM;
    int protocol = (hints && hints->ai_protocol) ? hints->ai_protocol : IPPROTO_TCP;

    struct my_ai *head = NULL;
    struct my_ai *prev = NULL;

    for (int i = 0; i < count; i++) {
        struct my_ai *entry = (struct my_ai *)calloc(1, sizeof(struct my_ai));
        if (!entry) break;
        entry->magic = MAGIC;
        entry->addr.sin.sin_family = AF_INET;
        entry->addr.sin.sin_port = htons((uint16_t)port);
        entry->addr.sin.sin_addr.s_addr = ips[i];

        entry->ai.ai_flags = 0;
        entry->ai.ai_family = AF_INET;
        entry->ai.ai_socktype = socktype;
        entry->ai.ai_protocol = protocol;
        entry->ai.ai_addrlen = sizeof(struct sockaddr_in);
        entry->ai.ai_addr = (struct sockaddr *)&entry->addr.sin;
        entry->ai.ai_canonname = NULL;
        entry->ai.ai_next = NULL;

        if (!head) head = entry;
        if (prev) prev->ai.ai_next = &entry->ai;
        prev = entry;
    }

    if (!head) {
        return EAI_MEMORY;
    }

    *res = &head->ai;
    return 0;
}

void freeaddrinfo(struct addrinfo *res) {
    init_orig();
    struct addrinfo *curr = res;
    while (curr) {
        struct my_ai *entry = (struct my_ai *)((char *)curr - offsetof(struct my_ai, ai));
        if (entry->magic == MAGIC) {
            struct addrinfo *next = curr->ai_next;
            entry->magic = 0;
            free(entry);
            curr = next;
        } else {
            orig_freeaddrinfo(curr);
            break;
        }
    }
}
