#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=/var/lib/postgresql/16/main
PGCONF=/etc/postgresql/16/main/postgresql.conf

# setpriv + exec, NOT `pty su -c ...`: both `unbuffer` (the pty helper) and `su` put the
# daemon in a fresh session, so it is reparented to init and supervisor's stopasgroup /
# killasgroup signal never reaches it. The daemon then survives `supervisorctl stop`,
# keeps holding port 5432, and the next start dies with "postmaster.pid already exists".
# setpriv drops privileges by exec'ing in place, so the postgres process keeps this PID
# and process group and stops when supervisor says so. Postgres logs unbuffered anyway,
# so nothing is lost by skipping the pty wrapper.
exec setpriv --reuid=postgres --regid=postgres --init-groups \
    "${PGBIN}/postgres" -D "${PGDATA}" -c config_file="${PGCONF}" 2>&1
