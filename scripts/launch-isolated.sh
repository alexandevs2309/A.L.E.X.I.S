#!/usr/bin/env bash
# Lanzador de ALEXIS AISLADO — deny-by-default real, tres capas de kernel.
#
# Por qué un script y no una nota en la documentación: el aislamiento depende de CÓMO se
# arranca el proceso. Si alguien lanza ALEXIS con otro comando, el net-ns no existe y nada
# avisa. Por eso esto es el punto de entrada, no un consejo.
#
# Las tres capas, y qué cubre cada una:
#
#   1. net-ns con cero rutas. No es «filtrado»: no hay a dónde ir. No hay lista de reglas
#      que mantener ni que un proceso pueda saltarse.
#   2. tmpfs sobre /run/systemd/resolve. SIN esto la salida por DNS SIGUE ABIERTA: el
#      stub 127.0.0.53 está unreachable, pero `nss-resolve` habla con systemd-resolved por
#      un socket Varlink **Unix**, y los sockets Unix no respetan el net-ns. Medido en esta
#      máquina: sin este tmpfs, `example.com` se resolvía DENTRO del aislamiento y
#      systemd-resolved —que vive en el namespace del host y sí tiene salida— reenvía esas
#      consultas al exterior. Es una vía de exfiltración de datos codificados en nombres.
#   3. PostgreSQL por socket Unix. No es una excepción de red: es un socket de fichero, así
#      que no abre ninguna vía de salida. Aquí está la diferencia entre «aislado» y
#      «aislado pero inservible».
#
# El bypass de libpq (que ignora el hook de auditoría de Python por ser C) NO se arregla
# aquí: se vuelve irrelevante, porque el kernel no le ofrece ningún destino.

set -euo pipefail

SOCKET_DIR="${ALEXIS_PG_SOCKET_DIR:-/home/auron/ALEXIS-pg-socket}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -S "$SOCKET_DIR/.s.PGSQL.5432" ]]; then
  echo "ERROR: no hay socket de PostgreSQL en $SOCKET_DIR" >&2
  echo "       Sin él, ALEXIS no puede persistir dentro del aislamiento, y abrir un hueco" >&2
  echo "       de red para llegar a PostgreSQL por TCP sería perder el aislamiento." >&2
  echo "       Comprueba: docker exec alexis-pg-a1 psql -U alexis -tAc 'show listen_addresses'" >&2
  exit 1
fi

export ALEXIS_PG_SOCKET_DIR="$SOCKET_DIR"
# El DSN va en formato URL, NO en formato «clave=valor» de libpq, y no es cosmético:
# `tests/db_isolation.py` deriva la base de test con `urlsplit`/`urlunsplit`, así que con
# un DSN de keyword/value produce una cadena de conexión corrupta y el aislamiento entre
# `alexis` y `alexis_test` deja de funcionar. En formato URL, el socket Unix va en el
# parámetro `host` —que es como libpq lo expresa— y el código existente sigue sirviendo.
# No se toca ese código: es la barrera que evita contaminar la base de desarrollo.
export ALEXIS_DATABASE_URL="${ALEXIS_DATABASE_URL:-postgresql://alexis:change-me@/alexis?host=$SOCKET_DIR}"

# `exec` en lugar de llamar: el proceso que queda es el ALEXIS real, no un hijo que
# hereda el namespace por casualidad. Si alguien lo lanza de otra forma, se pierde el
# aislamiento — y `tests/test_isolation_gate.py` está para que eso se note.
exec unshare --mount --net --map-root-user bash -c '
  set -euo pipefail
  mount --make-rprivate /
  mount -t tmpfs none /run/systemd/resolve
  # Levantar loopback NO abre ninguna salida. El lo de un namespace es un circuito
  # cerrado: el tráfico a 127.0.0.1 sólo alcanza procesos DENTRO de este namespace, nunca al
  # host ni a internet. Hace falta porque ALEXIS sirve su propia API HTTP en 127.0.0.1, y sin
  # esto la mitad de las pruebas morían con errores de urllib. Verificado: con lo arriba hay
  # 0 rutas y el aislamiento sigue intacto.
  ip link set lo up
  cd '"$REPO"'
  exec "$@"
' bash "$@"
