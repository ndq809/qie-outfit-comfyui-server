#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/environment.sh"

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=/var/lib/postgresql/16/main
PGCONF=/etc/postgresql/16/main/postgresql.conf

pty su -s /bin/bash postgres -c "${PGBIN}/postgres -D ${PGDATA} -c config_file=${PGCONF}" 2>&1
