// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Opinsys Oy 2026

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <avahi-client/client.h>
#include <avahi-client/publish.h>
#include <avahi-common/domain.h>
#include <avahi-common/error.h>
#include <avahi-common/simple-watch.h>

#define ALIAS "api.puu.local"

struct publisher {
    AvahiSimplePoll *poll;
    AvahiEntryGroup *group;
};

static void fail(struct publisher *publisher, const char *operation, int error)
{
    fprintf(stderr, "%s: %s: %s\n", ALIAS, operation, avahi_strerror(error));
    avahi_simple_poll_quit(publisher->poll);
}

static void group_callback(AvahiEntryGroup *group, AvahiEntryGroupState state,
                           void *userdata)
{
    struct publisher *publisher = userdata;

    switch (state) {
    case AVAHI_ENTRY_GROUP_ESTABLISHED:
        fprintf(stderr, "%s: alias established\n", ALIAS);
        break;
    case AVAHI_ENTRY_GROUP_COLLISION:
        fail(publisher, "alias collision", AVAHI_ERR_COLLISION);
        break;
    case AVAHI_ENTRY_GROUP_FAILURE:
        fail(publisher, "entry group failed",
             avahi_client_errno(avahi_entry_group_get_client(group)));
        break;
    case AVAHI_ENTRY_GROUP_UNCOMMITED:
    case AVAHI_ENTRY_GROUP_REGISTERING:
        break;
    }
}

static void publish_alias(AvahiClient *client, struct publisher *publisher)
{
    const char *fqdn;
    const char *next;
    uint8_t rdata[255];
    size_t size = 0;
    int error;

    if (!publisher->group) {
        publisher->group = avahi_entry_group_new(client, group_callback, publisher);
        if (!publisher->group) {
            fail(publisher, "create entry group", avahi_client_errno(client));
            return;
        }
    }
    if (!avahi_entry_group_is_empty(publisher->group))
        return;

    fqdn = avahi_client_get_host_name_fqdn(client);
    if (!fqdn) {
        fail(publisher, "get host FQDN", avahi_client_errno(client));
        return;
    }
    if (avahi_domain_equal(fqdn, ALIAS)) {
        fail(publisher, "alias equals host FQDN", AVAHI_ERR_COLLISION);
        return;
    }

    /* CNAME RDATA is an uncompressed DNS name. Avahi decodes escaped labels. */
    next = fqdn;
    while (*next) {
        char label[AVAHI_LABEL_MAX];
        size_t length;

        if (!avahi_unescape_label(&next, label, sizeof(label))) {
            fail(publisher, "decode host FQDN", AVAHI_ERR_INVALID_DOMAIN_NAME);
            return;
        }
        length = strlen(label);
        if (!length || size + length + 2 > sizeof(rdata)) {
            fail(publisher, "encode host FQDN", AVAHI_ERR_INVALID_DOMAIN_NAME);
            return;
        }
        rdata[size++] = (uint8_t)length;
        memcpy(rdata + size, label, length);
        size += length;
    }
    if (!size) {
        fail(publisher, "empty host FQDN", AVAHI_ERR_INVALID_DOMAIN_NAME);
        return;
    }
    rdata[size++] = 0;

    error = avahi_entry_group_add_record(
        publisher->group, AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC,
        AVAHI_PUBLISH_UNIQUE | AVAHI_PUBLISH_USE_MULTICAST, ALIAS,
        AVAHI_DNS_CLASS_IN, AVAHI_DNS_TYPE_CNAME, AVAHI_DEFAULT_TTL_HOST_NAME,
        rdata, size);
    if (error < 0) {
        fail(publisher, "add CNAME", error);
        return;
    }
    error = avahi_entry_group_commit(publisher->group);
    if (error < 0) {
        fail(publisher, "commit CNAME", error);
        return;
    }
    fprintf(stderr, "%s: publishing CNAME to %s\n", ALIAS, fqdn);
}

static void client_callback(AvahiClient *client, AvahiClientState state,
                            void *userdata)
{
    struct publisher *publisher = userdata;
    int error;

    switch (state) {
    case AVAHI_CLIENT_S_RUNNING:
        publish_alias(client, publisher);
        break;
    case AVAHI_CLIENT_S_COLLISION:
    case AVAHI_CLIENT_S_REGISTERING:
        /* Withdraw the old target until Avahi has established its new name. */
        if (publisher->group) {
            error = avahi_entry_group_reset(publisher->group);
            if (error < 0)
                fail(publisher, "reset CNAME", error);
        }
        break;
    case AVAHI_CLIENT_FAILURE:
        /* systemd reconnects us after daemon or D-Bus restarts. */
        fail(publisher, "Avahi client failed", avahi_client_errno(client));
        break;
    case AVAHI_CLIENT_CONNECTING:
        break;
    }
}

int main(void)
{
    struct publisher publisher = {0};
    AvahiClient *client;
    int error;

    publisher.poll = avahi_simple_poll_new();
    if (!publisher.poll) {
        fprintf(stderr, "%s: failed to create Avahi poll loop\n", ALIAS);
        return EXIT_FAILURE;
    }
    client = avahi_client_new(avahi_simple_poll_get(publisher.poll), 0,
                              client_callback, &publisher, &error);
    if (!client) {
        fprintf(stderr, "%s: connect to Avahi: %s\n", ALIAS, avahi_strerror(error));
    } else {
        if (avahi_simple_poll_loop(publisher.poll) < 0)
            perror("Avahi poll loop");
        avahi_client_free(client);
    }
    avahi_simple_poll_free(publisher.poll);
    return EXIT_FAILURE;
}
