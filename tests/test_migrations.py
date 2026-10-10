"""El sistema de migraciones: orden, idempotencia, cuarentena y fallo visible.

Antes de este cambio `scripts/migrations/*.sql` no lo ejecutaba nadie: eran documentación
en un directorio. Ahora `Database.migrate()` los aplica en orden y una sola vez.

La prueba que más importa aquí no es "se aplican", sino **qué NO se aplican**: una
migración con `DROP TABLE` sobre tablas que el código consulta no puede decidir un arranque
que la ejecute solo. Eso es lo que fija `test_una_migracion_destructiva_no_se_aplica`.
"""

from __future__ import annotations

import pytest

from alexis.storage.db import MIGRATIONS_DIR, QUARANTINED_MIGRATIONS, Database


async def _aplicadas(db) -> set[str]:
    filas = await db.fetch("SELECT migration_name FROM migrations_applied")
    return {f["migration_name"] for f in filas}


# --------------------------------------------------------------------- #
# 1 — Base vacía
# --------------------------------------------------------------------- #


async def test_migrate_aplica_el_esquema_base(db):
    """`schema.SQL` es la fuente de verdad del esquema y se sigue aplicando."""
    resultado = await db.migrate()

    assert resultado["failed"] is None
    tablas = {
        f["tablename"]
        for f in await db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert {"missions", "observations", "world_entities", "migrations_applied"} <= tablas


async def test_migrate_aplica_las_migraciones_pendientes(db):
    resultado = await db.migrate()

    assert resultado["applied"], "debe aplicar al menos las migraciones pendientes"
    aplicadas = await _aplicadas(db)
    for nombre in resultado["applied"]:
        assert nombre in aplicadas
    # Las que no son destructivas, todas.
    esperadas = set(Database.migration_files()) - set(QUARANTINED_MIGRATIONS)
    assert esperadas <= aplicadas, f"faltan por aplicar: {esperadas - aplicadas}"


async def test_las_migraciones_crean_su_schema(db):
    """Una migración aplicada tiene que haber hecho algo observable, no sólo registrarse."""
    await db.migrate()
    tablas = {
        f["tablename"]
        for f in await db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    # `060_goals` y `061_schedule_rules` crean estas; `061` la usa
    # `alexis/autonomy/schedule_rules.py`, o sea que no es decorativa.
    assert {"goals", "schedule_rules"} <= tablas, tablas


# --------------------------------------------------------------------- #
# 2 — Idempotencia
# --------------------------------------------------------------------- #


async def test_migrate_dos_veces_no_reaplica(db):
    """La segunda pasada no aplica nada nuevo: es lo que hace `schema.SQL` idempotente."""
    primera = await db.migrate()
    assert primera["applied"]

    aplicadas_tras_primera = await _aplicadas(db)
    segunda = await db.migrate()

    assert segunda["applied"] == [], "la segunda pasada no debe aplicar nada"
    assert segunda["failed"] is None
    assert await _aplicadas(db) == aplicadas_tras_primera


async def test_migrate_es_idempotente_todavia_con_migraciones_registradas(db):
    """Con la tabla ya poblada, repetir no duplica filas."""
    await db.migrate()
    await db.migrate()
    filas = await db.fetch("SELECT migration_name FROM migrations_applied")
    nombres = [f["migration_name"] for f in filas]
    assert len(nombres) == len(set(nombres)), "hay migraciones registradas dos veces"


# --------------------------------------------------------------------- #
# 3 — Fallo visible
# --------------------------------------------------------------------- #


async def test_una_migracion_que_falla_se_detiene_y_se_reporta(db, monkeypatch):
    """No se aplica en silencio ni se continúa sobre un estado a medias."""
    await db.migrate()
    antes = await _aplicadas(db)

    roto = "099_migracion_rota.sql"
    (MIGRATIONS_DIR / roto).write_text(
        "ESTO NO ES SQL VALIDO Y FALLA;", encoding="utf-8"
    )

    # Se captura ANTES de sustituir: si laclosure llamara a `Database.migration_files()`
    # ya estaría sustituida por sí misma.
    reales = Database.migration_files()

    def archivos():
        return sorted(set(reales) | {roto})

    monkeypatch.setattr(Database, "migration_files", staticmethod(archivos))
    try:
        resultado = await db.migrate()

        assert resultado["failed"] is not None, "un fallo debe reportarse"
        nombre, motivo = resultado["failed"]
        assert nombre == roto
        assert motivo, "el motivo del fallo no puede estar vacío"
        # Y la que falló NO queda registrada: si lo quedara, el próximo arranque la daría
        # por aplicada y el esquema quedaría a medias para siempre.
        assert roto not in await _aplicadas(db)
        assert await _aplicadas(db) == antes
    finally:
        (MIGRATIONS_DIR / roto).unlink()


async def test_el_resumen_informa_de_cuarentena(db):
    """La cuarentena es visible en el resumen, no es un silencio."""
    resultado = await db.migrate()
    assert resultado["quarantined"]
    for nombre in resultado["quarantined"]:
        assert resultado["quarantined"][nombre], "el motivo de la cuarentena debe existir"


# --------------------------------------------------------------------- #
# 4 — La cuarentena: lo que este arreglo protege
# --------------------------------------------------------------------- #


async def test_una_migracion_destructiva_no_se_aplica(db):
    """`063` hace DROP de tablas que `repositories.py` consulta.

    Si se aplicara sola, el Skill System se quedaría sin `skill_candidates` ni
    `skill_versions` y toda lectura de skills lanzaría "relation does not exist". Es la
    razón de que exista la cuarentena, y por eso tiene prueba propia.
    """
    await db.migrate()

    aplicadas = await _aplicadas(db)
    for nombre in QUARANTINED_MIGRATIONS:
        assert nombre not in aplicadas, (
            f"{nombre} se aplicó y elimina tablas que el código usa"
        )

    # Y la prueba de que la cuarentena está justificada: las tablas siguen ahí.
    tablas = {
        f["tablename"]
        for f in await db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert {"skill_candidates", "skill_versions", "skill_performance"} <= tablas, (
        "las tablas que lee el Skill System desaparecieron"
    )


async def test_la_cuarentena_declara_el_motivo():
    """Una cuarentena sin motivo escrito es una excepción sin explicación."""
    assert QUARANTINED_MIGRATIONS
    for nombre, motivo in QUARANTINED_MIGRATIONS.items():
        assert (MIGRATIONS_DIR / nombre).is_file(), f"{nombre} no existe"
        assert motivo and len(motivo) > 20, f"{nombre} sin motivo legible"


# --------------------------------------------------------------------- #
# 5 — Orden y descubrimiento
# --------------------------------------------------------------------- #


def test_las_migraciones_se_ordenan_por_prefijo_numerico():
    nombres = Database.migration_files()
    assert nombres == sorted(nombres)
    assert nombres[0].startswith("050")
    assert all(n.split("_")[0].isdigit() for n in nombres), nombres


def test_el_directorio_de_migraciones_no_depende_del_cwd(tmp_path, monkeypatch):
    """El arranque ocurre desde el servidor, los tests y la CLI: el CWD no es el mismo."""
    monkeypatch.chdir(tmp_path)
    assert MIGRATIONS_DIR.is_dir()
    assert Database.migration_files(), "cambiar de directorio no debe ocultar migraciones"


def test_una_migracion_nueva_se_descubre_sola():
    """Basta con soltar el fichero: no hay lista que mantener sincronizada a mano."""
    nombres = Database.migration_files()
    assert "064_add_memory_embeddings.sql" in nombres