# AISLAMIENTO REAL — APLICADO y verificado

> **Estado: APLICADO (2026-10-13).** Autorizado §4 de `PLAN-RECREACION.md`.
> **Ejecutar ALEXIS con `scripts/launch-isolated.sh`.** Sin ese lanzador NO hay aislamiento.

## Estado verificado

| Comprobación | Resultado |
|---|---|
| Pruebas de aceptación dentro del aislamiento | **15 passed** |
| Suite completa aislada | **1846 passed, 7 deselected, 0 failed** |
| Rutas de salida dentro del namespace | **0** |
| DNS dentro del namespace | **cerrado** |
| Internet TCP/IPv4/IPv6 | **bloqueado** |
| `libpq` (bypass de auditoría) | **sin destino** |
| PostgreSQL por TCP `127.0.0.1:5433` | **inalcanzable, listener cerrado** |
| Puertos publicados por el contenedor | **ninguno** |
| PostgreSQL por socket Unix | **569 misiones — persiste** |
| Datos de `alexis` tras la suite | **idénticos** (569/1463/1576/3) |
| Red del host | **intacta** |

Contenedor recreado con `listen_addresses=''` y socket Unix montado, sobre el **mismo
volumen** `e7d8f411…826ea`. El contenedor anterior **no se borró**: se renombró a
`alexis-pg-a1-retirado-20261013-014418` y sigue ahí, de modo que la reversión es
`docker stop alexis-pg-a1 && docker rm alexis-pg-a1 && docker rename <retirado> alexis-pg-a1 && docker start alexis-pg-a1`.

### Tres desviaciones respecto al plan previsto, todas justificadas

1. **`ip link set lo up` es obligatorio.** El plan no lo contemplaba y sin él la mitad de las
   pruebas moría: ALEXIS sirve su propia API HTTP en `127.0.0.1`, y el loopback de un
   namespace nuevo está DOWN. Subirlo **no abre ninguna salida** —el `lo` es un circuito
   cerrado: el tráfico a `127.0.0.1` sólo alcanza procesos del mismo namespace— y se
   comprobó que siguen haviendo 0 rutas con el aislamiento intacto.
2. **El directorio del socket quedó en modo `1775` en vez de `2775` con dueño `999`.** Sin
   sudo no se puede hacer `chown 999`. El bit sticky lo puso el entrypoint de la imagen. No
   es una apertura: `/home/auron` es `0750`, así que ningún otro usuario local puede ni
   llegar al directorio.
3. **El DSN pasó a formato URL** (`postgresql://<usuario>:<clave>@/alexis?host=/ruta/al/socket`) en vez
   de `clave=valor`. No es cosmético: `tests/db_isolation.py` deriva la base de test con
   `urlsplit`, y con un DSN de keyword/value generaba una cadena corrupta y **el aislamiento
   entre `alexis` y `alexis_test` dejaba de funcionar**. Se cambió el DSN, no el código: ese
   código es la barrera que evita contaminar la base de desarrollo.

### Tres tests marcados `external`

`test_egress_guard.py::test_06_el_dns_sigue_funcionando` y dos de `test_browser_tool.py`
exigen DNS real para distinguir «permitido» de «no verificable». En el despliegue aislado el
DNS no existe a propósito, así que su premisa es falsa: **no los se saltan, se reclasifican**.

Marcarlos fuera sin más sería **quitar seguridad sin sustituirla**. Por eso se añadieron dos
pruebas que la sustituyen y que sólo tienen sentido en el modo aislado:
`test_el_guardian_falla_cerrado_sin_dns` (sin resolución, `resolve_public` deniega con motivo
— no lanza excepción ni permite) y `test_el_browser_no_descarga_nada_sin_dns` (sin DNS, el
browser no pide **nada** a la red: pedir y luego descartar ya es haber transmitido).

Verificado que los tres siguen **pasando fuera** del aislamiento, donde sí hay red.

## 1. Qué hay que cerrar

El hook de auditoría de sockets de Python (`alexis/tools/net.py`) **no ve `libpq`**, que es
C y no genera evento de auditoría. Está documentado y es real. Cerrarlo exige una
decisión de kernel, no otra línea de Python.

## 2. Lo que se ha MEDIDO en esta máquina

Nada de esta sección es suposición; son mediciones repetibles.

| Hecho | Resultado |
|---|---|
| `unshare --net --map-root-user` sin más | 0 rutas, sólo `lo` (DOWN) |
| Internet (TCP/IPv4/IPv6) desde el net-ns | **bloqueado** |
| PostgreSQL por TCP (`127.0.0.1:5433`) desde el net-ns | **bloqueado** |
| **Socket Unix dentro del net-ns** | **funciona** |
| **DNS dentro del net-ns, sólo con el net-ns** | **FUNCIONA — fuga** |

## 3. El hallazgo que cambia el diseño

**Poner un espacio de nombres de red NO cierra la exfiltración por DNS.**

`/etc/resolv.conf` apunta a `127.0.0.53`, el stub de `systemd-resolved`. Con el net-ns
puesto, ese stub es inalcanzable (medido). **Pero `example.com` se resolvía igual.**

Motivo: `nss-resolve` no habla con el stub por TCP, habla con systemd-resolved por un
**socket Varlink Unix**, `/run/systemd/resolve/io.systemd.Resolve`, con permisos
`srw-rw-rw-`. Los sockets Unix **no respetan el espacio de nombres de red**. El proceso
aislado no tiene ruta, y aun así hacía consultas DNS que `systemd-resolved` —que vive en
el namespace del host y sí tiene salida— reenvía al exterior.

Medido en ambos sentidos:

```
dentro del net-ns, sin más:        example.com -> 2606:4700:10::6814:179a   (contestó)
dentro del net-ns + tmpfs:         example.com -> gaierror                  (cerrado)
fuera del namespace (control):     example.com -> 172.66.147.243            (normal)
```

Un nombre inexistente falla correctamente en los tres casos, así que no era una caché
local: **algo estaba respondiendo de verdad**.

Consecuencia: un tool de ALEXIS podría exfiltrar datos codificados en nombres de dominio
pese a tener cero rutas. Un diseño que sólo comprobara «no hay rutas» daría por buena una
fuga real.

## 4. El diseño

```
unshare --mount --net --map-root-user \
  bash -c 'mount --make-rprivate /; mount -t tmpfs none /run/systemd/resolve; exec ALEXIS'
```

Tres capas, todas en el kernel:

1. **Deny-by-default de verdad.** Un namespace con 0 rutas no es «filtrado»: no hay a
   dónde ir. No hay lista de reglas que mantener ni que se pueda saltar.
2. **DNS cerrado por las dos puertas.** El tmpfs sobre `/run/systemd/resolve` mata el
   Varlink; sin él, `nss-resolve` cae al `dns` módulo, que lee `resolv.conf`, apunta a
   `127.0.0.53` y falla porque `lo` está DOWN.
3. **La excepción que no cuesta nada.** PostgreSQL se alcanza por **socket Unix**
   (`PLAN-RECREACION.md` §4: `listen_addresses=''` + `-v $SOCK:/var/run/postgresql`). Un
   socket Unix no es red, así que **no abre ninguna vía de salida**. Ésta es toda la
   diferencia entre «aislado» y «aislado pero inservible».

### Por qué esto neutraliza el bypass de `libpq`

No lo arregla: `libpq` seguirá ignorando el hook de Python. Lo hace **irrelevante**, porque
el kernel no le ofrece ningún destino. `tests/test_isolation_gate.py::test_07` lo comprueba
con `psycopg` abriendo su propio socket.

## 5. Lo que este diseño NO da

| Límite | Consecuencia |
|---|---|
| Aísla el **proceso**, no la máquina | Otro proceso del host sigue saliendo con normalidad |
| **Depende de cómo se arranque** | Si alguien lanza ALEXIS sin el `unshare`, no hay aislamiento y nada avisa |
| Requiere `CAP_SYS_ADMIN` en el user-ns | Funciona sin root (verificado); no funciona en un runtime sin user-namespaces |
| PostgreSQL sigue publicly exposed en `0.0.0.0:5433` hasta §4 | El aislamiento protege a ALEXIS, **no** protege la base de la red local |

El segundo límite es el más importante: **es un control dependiente de configuración, no
intrínseco.** Por eso el gate verifica que el aislamiento está *activo*, no que fue
*configurado*.

## 6. Dependencias y reversión

- **Dependencia única e inevitable:** PostgreSQL debe exponer socket Unix. Sin él, dentro
  del aislamiento no hay persistencia y no hay alternativa — no se puede abrir un hueco de
  red sin perder el aislamiento. Esa dependencia es exactamente `PLAN-RECREACION.md` §4.
- **Reversión:** `docker start alexis-pg-a1` y volver al DSN TCP. Los datos no se tocan;
  viven en el mismo volumen `e7d8f411…826ea`.
- **El tmpfs no toca el host:** verificado — DNS del host sigue funcionando y el socket
  Varlink sigue existiendo fuera del namespace.

## 7. Pruebas de aceptación

`tests/test_isolation_gate.py`, 13 pruebas. **Hoy están en rojo a propósito**: definen el
estado que §4 debe producir. Ejecutadas sin aislamiento dan **10 fallos**; ése es el
comportamiento correcto de un gate, no una regresión.

Cada una tiene contraprueba:

| Prueba | Contraprueba | Demuestra |
|---|---|---|
| `test_01` sin rutas | fuera del ns → falla | deny-by-default real |
| `test_03` Varlink cerrado | quitar el tmpfs → falla | el bypass de DNS está cerrado |
| `test_07` libpq sin ruta | fuera del ns → falla | el bypass nativo es irrelevante |
| `test_09` PostgreSQL sin TCP | con `listen_addresses='*'` → falla | el socket Unix es la única puerta |

`test_07` Merece mención aparte: una primera versión afirmaba sólo «que la conexión
falla», y era un **falso positivo** — fuera del aislamiento también «fallaba», pero con
`connection timeout expired`, es decir los paquetes habían salido. Ahora distingue
`Network is unreachable` (correcto) de `timeout`/`refused` (fuga), y se comprobó que
falla fuera del aislamiento.
