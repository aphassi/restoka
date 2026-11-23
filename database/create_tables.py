"""Create Restoka database tables without modifying existing schemas."""

import argparse

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from database.database import make_engine
from database.models import Base


class SchemaMigrationRequired(RuntimeError):
    pass


def check_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())

    legacy = existing & {"pending_orders", "order_drafts", "action_log", "procurement_cases"}
    problems = [f"legacy table {name}" for name in sorted(legacy)]
    dialect = connection.dialect

    for name, table in Base.metadata.tables.items():
        if name not in existing:
            continue

        actual = {c["name"]: c for c in inspector.get_columns(name)}

        if set(actual) != set(table.columns.keys()):
            problems.append(f"{name}: columns differ")
            continue

        for column in table.columns:
            reflected = actual[column.name]

            if (str(reflected["type"].compile(dialect=dialect)) != str(column.type.compile(dialect=dialect))
                    or reflected["nullable"] != column.nullable):
                problems.append(f"{name}.{column.name}: type or nullability differs")
    
        if inspector.get_pk_constraint(name)["constrained_columns"] != [c.name for c in table.primary_key]:
            problems.append(f"{name}: primary key differs")

        expected_fk = {(tuple(c.parent.name for c in fk.elements),
                        tuple(c.column.name for c in fk.elements),
                        fk.referred_table.name, fk.ondelete) for fk in table.foreign_key_constraints}
        actual_fk = {(tuple(fk['constrained_columns']), tuple(fk['referred_columns']),
                      fk['referred_table'], fk.get('options', {}).get('ondelete'))
                     for fk in inspector.get_foreign_keys(name)}

        if expected_fk != actual_fk:
            problems.append(f"{name}: foreign keys differ")

        expected_unique = {tuple(c.name for c in constraint.columns) for constraint in table.constraints
                           if constraint.__class__.__name__ == 'UniqueConstraint'}
        actual_unique = {tuple(c['column_names']) for c in inspector.get_unique_constraints(name)}
        if expected_unique != actual_unique:
            problems.append(f"{name}: unique constraints differ")

        expected_checks = {c.name for c in table.constraints if c.__class__.__name__ == 'CheckConstraint'}
        if expected_checks != {c['name'] for c in inspector.get_check_constraints(name)}:
            problems.append(f"{name}: check constraints differ")

        expected_indexes = {i.name for i in table.indexes}
        actual_indexes = {i['name'] for i in inspector.get_indexes(name) if not i.get('duplicates_constraint')}
        if expected_indexes != actual_indexes:
            problems.append(f"{name}: indexes differ")

    if problems:
        raise SchemaMigrationRequired("Explicit schema migration required; no tables changed: " + "; ".join(problems))


def create_tables(database_url: str | None = None) -> list[str]:
    engine = make_engine(database_url)

    try:
        with engine.begin() as connection:
            check_schema(connection)
            Base.metadata.create_all(connection)
        return sorted(Base.metadata.tables)

    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Restoka database tables")
    parser.add_argument("--database-url", default=None, help="Overrides RESTOKA_DATABASE_URL")

    args = parser.parse_args()
    tables = create_tables(args.database_url)

    print(f"Schema ready: {len(tables)} tables")
    print("Tables:", ", ".join(tables))


if __name__ == "__main__":
    main()
