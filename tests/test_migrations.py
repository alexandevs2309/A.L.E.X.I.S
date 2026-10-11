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
    """Invariante: tras `migrate()`, toda migración noVeterle_aplicada queda registrada.

    No se afirma "esta llamada aplicó alguna": `migrations_applied` NO se vacía entre
    tests, así que la primera llamada del proceso aplica y las siguientes son no-ops. Lo que
    tiene que ser cierto siempre es el final, no cuántas se aplicaron en esta llamada.
    """
    resultado = await db.migrate()
    assert resultado["failed"] is None

    aplicadas = await _aplicadas(db)
    esperadas = set(Database.migration_files()) - set(QUARANTINED_MIGRATIONS)
    assert esperadas <= aplicadas, f"faltan por aplicar: {esperadas - aplicadas}"
    assert set(resultado["applied"]) <= aplicadas


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
    await db.migrate()
    aplicadas_tras_primera = await _aplicadas(db)
    assert aplicadas_tras_primera, "la primera pasada no registró nada"

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
    """La cuarentena sigue siendo visible aunque esté vacía.

    Vacía es el estado correcto desde que 063 se desbloqueó, pero el campo debe seguir
    existiendo: quien lea el resumen tiene que poder distinguir "no hay nada en cuarentena"
    de "la información no existe".
    """
    resultado = await db.migrate()
    assert "quarantined" in resultado
    assert isinstance(resultado["quarantined"], dict)
    for nombre, motivo in resultado["quarantined"].items():
        assert motivo, f"{nombre} está en cuarentena sin motivo escrito"


# --------------------------------------------------------------------- #
# 4 — La cuarentena: lo que este arreglo protege
# --------------------------------------------------------------------- #


async def test_063_ya_se_aplica_y_las_tablas_viejas_desaparecen(db):
    """`063` copia los datos y borra `skill_candidates`/`skill_versions`/`skill_performance`.

    Es el final de la consolidación: el Skill System escribe y lee sobre `skills`, así que
    las tablas viejas son estructura huérfana. Mantenerlas sería una segunda fuente de
    verdad que puede volver a desincronizarse, que es lo que consolidation evita.
    """
    resultado = await db.migrate()
    assert resultado["failed"] is None

    aplicadas = await _aplicadas(db)
    assert "063_skills_consolidation.sql" in aplicadas
    assert "063_skills_consolidation.sql" not in QUARANTINED_MIGRATIONS

    tablas = {
        f["tablename"]
        for f in await db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert "skills" in tablas
    assert not ({"skill_candidates", "skill_versions", "skill_performance"} & tablas), (
        "las tablas viejas siguen existiendo: la consolidación no ocurrió"
    )


async def test_una_migracion_destructiva_sigue_protegida_si_se_declara(db, monkeypatch):
    """El mecanismo de cuarentena sigue vivo aunque hoy no haya ninguna.

    Se prueba con una migración inventada que borra `missions`: si alguien añade un `DROP`
    sin justificarla, `migrate()` no debe aplicarla por su cuenta.
    """
    from alexis.storage.db import MIGRATIONS_DIR, QUARANTINED_MIGRATIONS as QM

    peligrosa = "098_peligrosa.sql"
    (MIGRATIONS_DIR / peligrosa).write_text("DROP TABLE IF EXISTS missions;", encoding="utf-8")
    monkeypatch.setitem(QM, peligrosa, "borraría la tabla de misiones")

    reales = Database.migration_files()

    def archivos():
        return sorted(set(reales) | {peligrosa})

    monkeypatch.setattr(Database, "migration_files", staticmethod(archivos))
    try:
        resultado = await db.migrate()
        assert resultado["applied"] == []
        assert resultado["quarantined"].get(peligrosa)
        tablas = {
            f["tablename"]
            for f in await db.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        assert "missions" in tablas, "una migración en cuarentena se aplicó igualmente"
    finally:
        (MIGRATIONS_DIR / peligrosa).unlink()


async def test_toda_migracion_en_cuarentena_declara_su_motivo():
    """Ninguna entrada sin motivo escrito: una cuarentena sin explicación es un veto invisible."""
    for nombre, motivo in QUARANTINED_MIGRATIONS.items():
        assert (MIGRATIONS_DIR / nombre).is_file(), f"{nombre} no existe en disco"
        assert motivo and len(motivo) > 20, f"{nombre} en cuarentena sin motivo legible"


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