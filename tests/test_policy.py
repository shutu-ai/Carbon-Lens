from __future__ import annotations

import pytest

from carbon_lens.policy import WritePolicyError, parse_write_sql


WORKSPACE = "carbon_lens_workspace"


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("CREATE TABLE carbon_lens_workspace.cl_tmp_a1 AS SELECT 1", "create_table_as_select"),
        ("-- comment\nCREATE TABLE `carbon_lens_workspace`.`cl_tmp_a2` AS WITH x AS (SELECT 1) SELECT * FROM x;", "create_table_as_select"),
        ("INSERT INTO TABLE carbon_lens_workspace.cl_tmp_a1 SELECT 1", "insert_into"),
        ("INSERT OVERWRITE TABLE carbon_lens_workspace.cl_tmp_a1 WITH x AS (SELECT 1) SELECT * FROM x", "insert_overwrite"),
        ("DROP TABLE carbon_lens_workspace.cl_tmp_a1", "drop_table"),
    ],
)
def test_allowed_write_subset_is_classified(sql: str, kind: str):
    plan = parse_write_sql(sql, WORKSPACE)
    assert plan.kind == kind
    assert plan.target.startswith(WORKSPACE + ".")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT",
        "INSERT INTO",
        "INSERT OVERWRITE",
        "DROP TABLE",
        "INSERT INTO business_schema.original_table SELECT 1",
        "INSERT OVERWRITE TABLE business_schema.original_table SELECT 1",
        "UPDATE business_schema.original_table SET x = 1",
        "DELETE FROM business_schema.original_table",
        "MERGE INTO business_schema.original_table t USING x s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.x=s.x",
        "DROP TABLE business_schema.original_table",
        "ALTER TABLE business_schema.original_table ADD COLUMNS (x INT)",
        "TRUNCATE TABLE business_schema.original_table",
        "RENAME TABLE business_schema.original_table TO carbon_lens_workspace.cl_tmp_x",
        "CREATE OR REPLACE TABLE business_schema.original_table AS SELECT 1",
        "CREATE TABLE IF NOT EXISTS carbon_lens_workspace.cl_tmp_x AS SELECT 1",
        "CREATE TABLE carbon_lens_workspace.cl_tmp_x AS SELECT 1; DROP TABLE business_schema.original_table",
        "INSERT OVERWRITE DIRECTORY '/tmp/out' SELECT 1",
        "USE carbon_lens_workspace",
        "SET x=y",
        "CREATE TABLE carbon_lens_workspace.cl_tmp_x STORED AS PARQUET AS SELECT 1",
        "CREATE TABLE carbon_lens_workspace.cl_tmp_x AS SELECT 1 LOCATION '/tmp/out'",
    ],
)
def test_unsafe_or_ambiguous_write_is_rejected(sql: str):
    with pytest.raises(WritePolicyError):
        parse_write_sql(sql, WORKSPACE)


def test_read_source_with_literal_semicolon_is_not_split():
    plan = parse_write_sql(
        "CREATE TABLE carbon_lens_workspace.cl_tmp_literal AS SELECT 'a; b' AS value",
        WORKSPACE,
    )
    assert plan.source_sql == "SELECT 'a; b' AS value"


def test_forbidden_words_inside_literals_do_not_trigger_policy_rejection():
    plan = parse_write_sql(
        "CREATE TABLE carbon_lens_workspace.cl_tmp_words AS SELECT 'DROP TABLE' AS value",
        WORKSPACE,
    )
    assert plan.kind == "create_table_as_select"


def test_unqualified_and_other_workspace_targets_fail_closed():
    for sql in (
        "CREATE TABLE cl_tmp_x AS SELECT 1",
        "INSERT INTO other_workspace.cl_tmp_x SELECT 1",
        "DROP TABLE other_workspace.cl_tmp_x",
    ):
        with pytest.raises(WritePolicyError):
            parse_write_sql(sql, WORKSPACE)
