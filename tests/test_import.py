"""CSV 导入测试：列名冲突、类型推断、事务原子性、参数化安全。"""
from __future__ import annotations

import sqlite3

from tests.base import TempDataTestCase
from app.errors import ValidationError
from app.importer import import_csv, list_tables, parse_and_validate


class ImportValidationTests(TempDataTestCase):
    def test_basic_import_types(self):
        csv_bytes = "id,name,score,note\n1,Alice,9.5,\n2,Bob,8,hello\n".encode()
        result = import_csv(self.space, csv_bytes, "students")
        self.assertEqual(result["row_count"], 2)
        types = {c["name"]: c["type"] for c in result["columns"]}
        self.assertEqual(types["id"], "INTEGER")
        self.assertEqual(types["name"], "TEXT")
        self.assertEqual(types["score"], "REAL")
        self.assertEqual(types["note"], "TEXT")  # 全 NULL 列退化为 TEXT

    def test_duplicate_column_names_rejected(self):
        csv_bytes = "a,a,b\n1,2,3\n".encode()
        with self.assertRaises(ValidationError) as ctx:
            import_csv(self.space, csv_bytes, "t")
        self.assertIn("列名冲突", str(ctx.exception))

    def test_case_insensitive_column_conflict(self):
        csv_bytes = "Foo,FOO\n1,2\n".encode()
        with self.assertRaises(ValidationError):
            import_csv(self.space, csv_bytes, "t")

    def test_keyword_table_name_rejected(self):
        with self.assertRaises(ValidationError):
            parse_and_validate(b"a\n1\n", "select")

    def test_bad_identifier_rejected(self):
        with self.assertRaises(ValidationError):
            parse_and_validate(b"a\n1\n", "bad-name")
        with self.assertRaises(ValidationError):
            parse_and_validate(b"a\n1\n", "1start")

    def test_inconsistent_field_count(self):
        csv_bytes = "a,b,c\n1,2,3\n4,5\n".encode()
        with self.assertRaises(ValidationError) as ctx:
            import_csv(self.space, csv_bytes, "t")
        # 表头是第 1 行，因此错误行号为第 3 行
        self.assertIn("第 3 行", str(ctx.exception))

    def test_empty_and_header_only_rejected(self):
        with self.assertRaises(ValidationError):
            import_csv(self.space, b"", "t")
        with self.assertRaises(ValidationError):
            import_csv(self.space, b"a,b\n", "t")

    def test_duplicate_table_name_rejected(self):
        import_csv(self.space, b"a\n1\n", "dup_t")
        with self.assertRaises(ValidationError):
            import_csv(self.space, b"a\n2\n", "DUP_T")  # 忽略大小写

    def test_type_promotion_integer_to_real(self):
        csv_bytes = "v\n1\n2.5\n".encode()
        parsed = parse_and_validate(csv_bytes, "t")
        self.assertEqual(parsed.col_types[0], "REAL")

    def test_injection_in_csv_content_is_parameterized(self):
        # CSV 里的恶意字符串不会被当作 SQL 执行
        csv_bytes = (
            "name\n"
            "Robert'); DROP TABLE students;--\n"
            "正常名字\n"
        ).encode()
        import_csv(self.space, csv_bytes, "students")
        tables = list_tables(self.space)
        self.assertEqual([t["name"] for t in tables], ["students"])
        conn = sqlite3.connect(self.space.data_db)
        rows = conn.execute("SELECT name FROM students ORDER BY name").fetchall()
        conn.close()
        self.assertIn("Robert'); DROP TABLE students;--", [r[0] for r in rows])

    def test_failed_import_leaves_no_table(self):
        # 先建一张表
        import_csv(self.space, b"a\n1\n", "keep_me")
        # 导入失败（列数不一致）：不能留下半张新表
        with self.assertRaises(ValidationError):
            import_csv(self.space, b"x,y\n1,2\n3\n", "half_table")
        tables = {t["name"] for t in list_tables(self.space)}
        self.assertEqual(tables, {"keep_me"})

    def test_bom_utf8_supported(self):
        csv_bytes = "﻿id,name\n1,张三\n".encode("utf-8")
        result = import_csv(self.space, csv_bytes, "bom_t")
        self.assertEqual(result["columns"][0]["name"], "id")

    def test_non_utf8_rejected(self):
        with self.assertRaises(ValidationError):
            import_csv(self.space, "a\n中文\n".encode("gbk"), "enc_t")

    def test_table_listing_shows_schema_and_count(self):
        import_csv(self.space, b"id,val\n1,x\n2,y\n3,z\n", "listed")
        tables = list_tables(self.space)
        self.assertEqual(len(tables), 1)
        t = tables[0]
        self.assertEqual(t["row_count"], 3)
        self.assertEqual([c["name"] for c in t["columns"]], ["id", "val"])

    def test_drop_table(self):
        from app.importer import drop_table
        import_csv(self.space, b"a\n1\n", "drop_me")
        drop_table(self.space, "drop_me")
        self.assertEqual(list_tables(self.space), [])

    def test_drop_invalid_identifier(self):
        from app.importer import drop_table
        with self.assertRaises(ValidationError):
            drop_table(self.space, 'x; DROP TABLE y')
