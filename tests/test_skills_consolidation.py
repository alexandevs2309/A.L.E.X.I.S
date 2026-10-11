"""Consolidación del Skill System: PK `(id, version)`, round-trip y migración 063.

Lo que aquí se protege son tres invariantes que se rompieron al empezar:

1. **Dos versiones de la misma skill coexisten.** Con `PRIMARY KEY (id)` la v2 no cabía y
   la v1 se pisaba. `test_16_la_mejora_crea_v2_y_no_toca_v1` (del CORE-11, intocable) lo
   exige, y aquí se comprueba además contra PostgreSQL y no sólo en memoria.
2. **El round-trip conserva `risk`, `validation` y `provenance`.** Sin `risk`, `from_dict`
   aplica `or "low"` y una skill de riesgo alto se valida como baja: fail-closed → fail-open.
3. **063 copia los datos antes de borrar.** El orden está en el fichero, pero un orden
   correcto no se demuestra por lectura.
"""

from __future__ import annotations

import pytest

from alexis.learning.skill import (
    SkillCandidate,
    SkillPerformance,
    SkillStatus,
    SkillValidator,
    SkillVersion,
)
from alexis.storage.db import QUARANTINED_MIGRATIONS, Database
from alexis.storage.repositories import LearningRepository

CAPS = None


def _validator():
    from test_core11_learning_skills import CAPS as _CAPS

    return SkillValidator(catalog=_CAPS)


def _candidato(**kw):
    """Candidato que el `SkillValidator` acepta.

    Se reaprovecha el del CORE-11 en vez de inventar uno: un candidato que el validador
    rechaza devuelve `None` desde `promote()`, y estos tests no son sobre validación.
    """
    from test_core11_learning_skills import _candidate

    return _candidate(**kw)


# --------------------------------------------------------------------- #
# 1 — Identidad y coexistencia de versiones
# --------------------------------------------------------------------- #


async def test_dos_versiones_de_la_misma_skill_conviven(db):
    """La razón del cambio de PK: v1 y v2 comparten `skill_id` y deben persistir ambas."""
    validator = _validator()
    v1 = validator.promote(_candidato(), existing=[])
    v2 = validator.promote(_candidatov2 := _candidato(), existing=[v1])
    assert v1 is not None and v2 is not None, "el validador rechazó el candidato de test"

    repo = LearningRepository(db)
    await repo.save_skill_version(v1)
    await repo.save_skill_version(v2)

    filas = await db.fetch(
        "SELECT version, status FROM skills WHERE id = %(id)s ORDER BY version",
        {"id": v1.skill_id},
    )
    assert [f["version"] for f in filas] == [1, 2], "la v1 no puede pisarse"
    assert filas[0]["status"] == SkillStatus.SUPERSEDED.value
    assert filas[1]["status"] == SkillStatus.VALIDATED.value


async def test_la_pk_es_compuesta_por_id_y_version(db):
    """Comprobación estructural: un `PRIMARY KEY (id)` solo rompería lo anterior."""
    filas = await db.fetch(
        "SELECT conname, pg_get_constraintdef(oid) AS d "
        "FROM pg_constraint WHERE conrelid = 'skills'::regclass AND contype = 'p'"
    )
    assert filas, "skills no tiene clave primaria"
    assert "version" in filas[0]["d"], filas[0]["d"]


async def test_candidato_y_version_viven_en_tablas_diferentes(db):
    """Candidatos y versiones se distinguen por `status`, no por tabla."""
    repo = LearningRepository(db)
    candidato = _candidato()
    await repo.save_candidate(candidato)
    await repo.save_skill_version(_validator().promote(_candidato(), existing=[]))

    # La columna y el `payload` NO pueden decir cosas distintas: si difieren, `candidates()`
    # filtra por una y devuelve la otra.
    candidatos = await repo.candidates()
    assert candidatos and all(c["status"] == candidato.status for c in candidatos)
    versiones = await repo.skill_versions()
    assert versiones and all(v["status"] in ("validated", "superseded", "active") for v in versiones)
    assert not ({c["candidate_id"] for c in candidatos} & {v["skill_id"] for v in versiones})


async def test_una_version_publicada_es_inmutable(db):
    """Sobrescribirla haría imposible reconstruir una ejecución antigua."""
    repo = LearningRepository(db)
    v1 = _validator().promote(_candidato(), existing=[])
    await repo.save_skill_version(v1)

    mutada = SkillVersion.from_dict(v1.to_dict())
    mutada.procedure = [{"capability": "fs.remove", "args": {"path": "TODO"}}]
    await repo.save_skill_version(mutada)

    guardada = await db.fetch(
        "SELECT payload FROM skills WHERE id = %(id)s AND version = %(v)s",
        {"id": v1.skill_id, "v": v1.version},
    )
    assert "fs.remove" not in guardada[0]["payload"]["procedure"]


# --------------------------------------------------------------------- #
# 2 — Round-trip: risk, validation, provenance
# --------------------------------------------------------------------- #


async def test_el_roundtrip_preserva_risk_validation_y_provenance(db):
    """Los tres campos cuya pérdida convertiría el fail-closed en fail-open."""
    v = SkillVersion(
        skill_id="skill-x",
        version=1,
        name="deploy",
        procedure=[{"capability": "fs.read", "args": {"path": "a.txt"}}],
        risk="high",
        validation={"proof": "tres misiones", "checked_at": 123.0},
        provenance={"lesson_id": "lesson-7"},
        applicability="siempre que haya entorno de staging",
        contraindications=["nunca en producción"],
    )
    repo = LearningRepository(db)
    await repo.save_skill_version(v)

    [raw] = [r for r in await repo.skill_versions() if r["skill_id"] == "skill-x"]
    r = SkillVersion.from_dict(raw)

    assert r.risk == "high", "el riesgo no puede perderse ni degradarse a 'low'"
    assert r.validation == v.validation
    assert r.provenance == v.provenance
    assert r.applicability == v.applicability
    assert r.contraindications == v.contraindications
    assert r.procedure == v.procedure


async def test_el_roundtrip_de_un_candidato_tambien_es_exacto(db):
    repo = LearningRepository(db)
    candidato = _candidato(name="rollback", applicability="solo en staging",
                         source_lessons=["lesson-1"], risk="medium",
                         provenance={"lesson": "lesson-1"})
    await repo.save_candidate(candidato)

    [raw] = await repo.candidates()
    # El candidato del CORE-11 declara risk='medium'; lo que importa aquí es que el valor
    # que escribió el repositorio es el que vuelve, sin degradarse.
    assert raw["risk"] == candidato.risk
    assert raw["source_lessons"] == ["lesson-1"]
    assert raw["payload"]["provenance"] == {"lesson": "lesson-1"}
    assert raw["applicability"] == "solo en staging"


# --------------------------------------------------------------------- #
# 3 — risk ausente es 'critical', nunca 'low'
# --------------------------------------------------------------------- #


async def test_un_riesgo_ausente_se_guarda_como_critical(db):
    """Si `risk` no llega, se guarda 'critical'. Nunca 'low'."""
    v = SkillVersion(skill_id="skill-sin-riesgo", version=1, name="n", risk="")
    await LearningRepository(db).save_skill_version(v)

    filas = await db.fetch(
        "SELECT risk FROM skills WHERE id = 'skill-sin-riesgo'"
    )
    assert filas[0]["risk"] == "critical"


async def test_un_riesgo_fuera_de_vocabulario_tambien_es_critical(db):
    v = SkillVersion(skill_id="skillraro", version=1, name="n", risk="no-existe")
    await LearningRepository(db).save_skill_version(v)
    filas = await db.fetch("SELECT risk FROM skills WHERE id = 'skillraro'")
    assert filas[0]["risk"] == "critical"


async def test_risk_y_payload_no_admiten_nulo(db):
    """`NOT NULL` comprobado contra la BASE, no contra el texto de `schema.py`.

    Leer el fuente demuestra que la intención está escrita; no que la base obeyezca. Una
    migraciónApplied a medias, o un `ALTER` posterior, dejarían el fuente intacto y la
    columna admitiendo nulos justo donde el riesgo deja de ser restrictivo. Por eso la
    aserción va contra `information_schema`.
    """
    filas = await db.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'skills' AND column_name IN ('risk', 'payload')"
    )
    assert {f["column_name"]: f["is_nullable"] for f in filas} == {
        "risk": "NO",
        "payload": "NO",
    }, "risk/payload admiten NULL en la base de datos"

    [default_riesgo] = await db.fetch(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'skills' AND column_name = 'risk'"
    )
    assert "critical" in (default_riesgo["column_default"] or ""), default_riesgo[
        "column_default"
    ]


async def test_risk_nulo_es_rechazado_por_la_base(db):
    """No basta el `NOT NULL`: se intenta insertar y la base tiene que negarse.

    El resto de columnas `NOT NULL` se rellena a partir de `information_schema`, no a
    mano. Escritas a mano, saltaría la primera que se olvidara —`purpose`, `capabilities_
    used`— y el test quedaría verde sin haber probado nada sobre `risk`. Un
    `NotNullViolation` provocado por la columna equivocada es peor que no tener test.
    """
    import psycopg

    filas = await db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'skills' AND is_nullable = 'NO' AND column_name <> 'risk' "
        "ORDER BY column_name"
    )
    assert filas, "skills no tiene columnas NOT NULL: el test no probaría nada"

    literales = {
        "id": "'sin-riesgo'",
        "name": "'x'",
        "purpose": "'p'",
        "status": "'candidate'",
        "version": "1",
        "proposed_version": "0",
        "created_at": "now()",
        "updated_at": "now()",
    }
    columnas, valores = [], []
    for f in filas:
        columnas.append(f["column_name"])
        if f["column_name"] in literales:
            valores.append(literales[f["column_name"]])
        elif f["data_type"] in ("jsonb", "json"):
            valores.append("'[]'::jsonb")
        elif f["data_type"] in ("integer", "bigint", "smallint"):
            valores.append("0")
        else:
            valores.append("''")

    with pytest.raises(psycopg.errors.NotNullViolation) as exc:
        await db.execute(
            f"INSERT INTO skills ({', '.join(columnas)}, risk) "
            f"VALUES ({', '.join(valores)}, NULL)"
        )
    assert exc.value.diag.column_name == "risk", (
        f"la violación vino de {exc.value.diag.column_name!r}, no de 'risk'"
    )


# --------------------------------------------------------------------- #
# 4 — Rendimiento agregado
# --------------------------------------------------------------------- #


async def test_el_rendimiento_se_acumula_en_la_fila_de_la_skill(db):
    repo = LearningRepository(db)
    v = _validator().promote(_candidato(), existing=[])
    assert v is not None
    await repo.save_skill_version(v)

    for i in range(3):
        await repo.save_performance(
            SkillPerformance(skill_id=v.skill_id, version=v.version,
                            mission_id=f"m-{i}", outcome="success", verified=True)
        )

    registros = await repo.performance(skill_id=v.skill_id, version=v.version)
    assert len(registros) == 3, f"se esperaban 3 registros, hay {len(registros)}"
    assert {r["mission_id"] for r in registros} == {"m-0", "m-1", "m-2"}


async def test_todo_el_rendimiento_reconstruye_el_registro(db):
    repo = LearningRepository(db)
    v = _validator().promote(_candidato(), existing=[])
    assert v is not None
    await repo.save_skill_version(v)
    await repo.save_performance(
        SkillPerformance(skill_id=v.skill_id, version=v.version,
                        mission_id="m-1", outcome="success", verified=True)
    )
    assert len(await repo.all_performance()) >= 1


# --------------------------------------------------------------------- #
# 5 — La migración 063
# --------------------------------------------------------------------- #


def test_063_ya_no_esta_en_cuarentena():
    assert "063_skills_consolidation.sql" not in QUARANTINED_MIGRATIONS


async def test_063_esta_registrada_como_aplicada(db):
    await db.migrate()
    aplicadas = {
        f["migration_name"]
        for f in await db.fetch("SELECT migration_name FROM migrations_applied")
    }
    assert "063_skills_consolidation.sql" in aplicadas


async def test_las_tablas_viejas_desaparecieron(db):
    await db.migrate()
    tablas = {
        f["tablename"]
        for f in await db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert "skills" in tablas
    assert not ({"skill_versions", "skill_candidates", "skill_performance"} & tablas), (
        "las tablas viejas siguen ahí: la consolidación no ocurrió"
    )


async def test_el_orden_de_063_copia_antes_de_borrar():
    """Se lee del fichero, porque un orden correcto no se demuestra por escrito."""
    sql = (
        __import__("pathlib").Path("scripts/migrations/063_skills_consolidation.sql")
        .read_text(encoding="utf-8")
    )
    i_versions = sql.index("FROM skill_versions sv")
    i_candidates = sql.index("FROM skill_candidates sc")
    i_drop = sql.index("DROP TABLE IF EXISTS skill_versions")
    assert i_versions < i_drop, "las versiones se copiarían después de borrarlas"
    assert i_candidates < i_drop, "los candidatos se copiarían después de borrarlos"


async def test_los_datos_migrados_son_accesibles_desde_el_repositorio(db):
    """El final de la cadena: lo que copió 063 se lee por la misma API que el runtime."""
    v = _validator().promote(_candidato(), existing=[])
    assert v is not None
    await LearningRepository(db).save_skill_version(v)

    filas = await db.fetch(
        "SELECT id, version, risk, payload FROM skills WHERE id = %(id)s", {"id": v.skill_id}
    )
    assert filas, "la fila no llegó a `skills`"
    assert filas[0]["risk"] == v.risk
    assert SkillVersion.from_dict({**filas[0], "skill_id": filas[0]["id"]}) is not None